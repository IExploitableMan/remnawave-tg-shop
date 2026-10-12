import asyncio
import copy
import fnmatch
import json
import os
import signal
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import asyncssh
from sqlalchemy import select
from db.models import InfrastructureNode as Node, InfrastructureDeployment as Deployment, InfrastructureJob as Job
from .settings import read_config


async def host_key(address, port):
    key = await asyncio.wait_for(asyncssh.get_server_host_key(address, port=port), timeout=15)
    if not key:
        raise ValueError('SSH-сервер не вернул ключ')
    return key.export_public_key().decode().strip(), key.get_fingerprint()


def ssh_arguments(deployment, settings, password=None):
    # Only the pinned key is trusted, including after replacement/reinstallation.
    key = asyncssh.import_public_key(deployment.host_public_key)
    return dict(host=deployment.address, port=deployment.ssh_port,
        username=deployment.ssh_user, password=password,
        client_keys=[settings.INFRA_SSH_PRIVATE_KEY] if settings.INFRA_SSH_PRIVATE_KEY else [],
        known_hosts=([key], [], []), connect_timeout=20, login_timeout=20)


async def ansible(service, deployment, config, kind='provision', secret=None):
    password = service.cipher().decrypt(deployment.credentials.encode()).decode()
    root = Path(service.settings.INFRA_ARTIFACT_DIR)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    public_keys = [entry['key'] for entry in config['ssh_keys']]
    if service.settings.INFRA_SSH_PRIVATE_KEY:
        key = asyncssh.read_private_key(service.settings.INFRA_SSH_PRIVATE_KEY)
        public_keys.append(key.export_public_key().decode().strip())
    with tempfile.TemporaryDirectory(prefix='ansible-', dir=root) as temporary:
        directory = Path(temporary)
        host = f'[{deployment.address}]:{deployment.ssh_port}' if deployment.ssh_port != 22 else deployment.address
        known_hosts = directory / 'known_hosts'
        known_hosts.write_text(f'{host} {deployment.host_public_key}\n')
        inventory = {'all': {'hosts': {'node': {'ansible_host': deployment.address,
            'ansible_port': deployment.ssh_port, 'ansible_user': deployment.ssh_user,
            'ansible_password': password, 'ansible_become_password': password,
            'ansible_ssh_common_args': f'-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts}',
            'ssh_public_keys': public_keys, 'panel_ip': config['panel_ip'],
            'node_version': deployment.node_version, 'node_secret': secret}}}}
        if service.settings.INFRA_SSH_PRIVATE_KEY:
            inventory['all']['hosts']['node']['ansible_ssh_private_key_file'] = service.settings.INFRA_SSH_PRIVATE_KEY
        path = directory / 'inventory.json'
        path.write_text(json.dumps(inventory)); path.chmod(0o600)
        env = dict(os.environ, ANSIBLE_HOST_KEY_CHECKING='True', ANSIBLE_NOCOLOR='1',
                   ANSIBLE_RETRY_FILES_ENABLED='False', ANSIBLE_LOG_PATH='/dev/null',
                   ANSIBLE_SSH_PASSWORD_MECHANISM='sshpass')
        process = await asyncio.create_subprocess_exec('python', '-m', 'ansible.cli.playbook',
            '-i', str(path), str(Path(__file__).resolve().parents[3] / 'infra' / 'ansible' / f'{kind}.yml'),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, start_new_session=True)
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout=1800)
            if process.returncode:
                import re
                tasks = re.findall(r'TASK \[([^\]]+)\]', output.decode(errors='replace'))
                failed_task = tasks[-1] if tasks else 'initialization'
                raise RuntimeError(f'Ansible: код {process.returncode}, этап {failed_task}; секретный вывод не сохранён')
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()


async def stage(service, job_id, value):
    async with service.factory() as session:
        row = await session.get(Job, job_id)
        row.stage = value
        await session.commit()


async def provision(service, node, deployment, config, job_id):
    api = service.api
    inbound_data = await service.panel.get_inbounds_by_profile_uuid(deployment.profile_uuid, refresh=True)
    if inbound_data is None:
        raise RuntimeError('Панель недоступна: не удалось получить выбранный inbound')
    inbound = next((i for i in inbound_data or [] if i['uuid'] == deployment.inbound_uuid), None)
    if not inbound or int(inbound.get('port') or 0) != 443 or inbound.get('type', '').lower() != 'vless':
        raise ValueError('Выбранный inbound должен быть VLESS на порту 443')
    raw = inbound.get('rawInbound') or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    reality = (raw.get('streamSettings') or {}).get('realitySettings')
    if deployment.sni and reality and not any(fnmatch.fnmatchcase(deployment.sni, name)
                                             for name in reality.get('serverNames', [])):
        # Never rewrite the common profile or add inbounds to every squad.
        raise ValueError('SNI отсутствует в serverNames выбранного REALITY inbound. Добавьте допустимые SNI в профиль.')
    await stage(service, job_id, 'ansible')
    secret = await api.node_secret()
    await ansible(service, deployment, config, secret=secret)
    await stage(service, job_id, 'panel_node')
    config_profile = {'activeConfigProfileUuid': deployment.profile_uuid, 'activeInbounds': [deployment.inbound_uuid]}
    values = {'address': deployment.address, 'port': 2222, 'countryCode': node.country,
              'providerUuid': node.provider_uuid, 'configProfile': config_profile}
    # Panel node UUID is recorded before switching clients; replay finds it by
    # the allocated globally unique name if the create response was lost.
    old_node = next((n for n in await api.nodes() if n['uuid'] == node.panel_uuid), None) if node.panel_uuid else None
    if not node.panel_uuid:
        existing = next((n for n in await api.nodes() if n['name'] == node.name), None)
        if existing:
            cp = existing.get('configProfile') or {}
            inbounds = [i['uuid'] if isinstance(i, dict) else i for i in cp.get('activeInbounds', [])]
            if (existing.get('address') != deployment.address or existing.get('countryCode') != node.country
                    or cp.get('activeConfigProfileUuid') != deployment.profile_uuid
                    or set(inbounds) != {deployment.inbound_uuid}):
                raise ValueError('Имя уже занято другой нодой панели. Установка не будет менять её адрес или профиль.')
        result = existing or await api.call('POST', '/nodes', dict(values, name=node.name))
        node.panel_uuid = result['uuid']
        async with service.factory() as session:
            row = await session.get(Node, node.id); row.panel_uuid = node.panel_uuid
            await session.commit()
    else:
        await api.patch_node(node.panel_uuid, values)
    try:
        await stage(service, job_id, 'connection_check')
        for attempt in range(30):
            data = await api.call('GET', f'/nodes/{node.panel_uuid}')
            if data.get('isConnected') and not data.get('isConnecting'):
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError('Панель не подключилась к новой ноде за 60 секунд')
        await stage(service, job_id, 'host')
        hosts = await service.panel.get_all_hosts()
        if hosts is None:
            raise RuntimeError('Не удалось получить список hosts')
        old_host = next((h for h in hosts if h['uuid'] == node.host_uuid), None)
        values = {'address': deployment.host_address, 'port': 443, 'nodes': [node.panel_uuid],
                  'inbound': {'configProfileUuid': deployment.profile_uuid,
                              'configProfileInboundUuid': deployment.inbound_uuid}}
        if deployment.sni:
            values['sni'] = deployment.sni
        if not node.host_uuid:
            values['isDisabled'] = True
        if not node.host_uuid:
            candidate = next((h for h in hosts if h.get('nodes') == [node.panel_uuid] and h['remark'] == node.name), None)
            result = candidate or await api.call('POST', '/hosts', dict(values, remark=node.name))
            node.host_uuid = result['uuid']
            async with service.factory() as session:
                row = await session.get(Node, node.id); row.host_uuid = node.host_uuid
                await session.commit()
        else:
            await api.call('PATCH', '/hosts', dict(values, uuid=node.host_uuid))
        # A failed post-switch check rolls addresses back. No root password or
        # server wipe is involved in this compensation.
        from .diagnostics import quick_check
        await quick_check(service, deployment, node)
    except BaseException:
        if old_node:
            rollback = {k: old_node[k] for k in ['address', 'port', 'countryCode', 'providerUuid'] if k in old_node}
            cp = old_node.get('configProfile') or {}
            rollback['configProfile'] = {'activeConfigProfileUuid': cp.get('activeConfigProfileUuid'),
                'activeInbounds': [i['uuid'] if isinstance(i, dict) else i for i in cp.get('activeInbounds', [])]}
            await api.patch_node(node.panel_uuid, rollback)
        if 'old_host' in locals() and old_host:
            rollback_host = {k: old_host[k] for k in ['address', 'port', 'nodes', 'inbound', 'sni'] if k in old_host}
            await api.call('PATCH', '/hosts', dict(rollback_host, uuid=node.host_uuid))
        raise
    # Once traffic has switched, persist the matching credentials immediately.
    # Billing API failure must not leave the card pointing at the old VPS.
    async with service.factory() as session:
        previous = (await session.execute(select(Deployment).where(Deployment.node_id == node.id,
            Deployment.state == 'active', Deployment.id != deployment.id))).scalars().all()
        for item in previous:
            item.state = 'replaced'
        (await session.get(Deployment, deployment.id)).state = 'active'
        await session.commit()
    if not old_node:
        await api.call('PATCH', '/hosts', {'uuid': node.host_uuid, 'isDisabled': False})
    await service.publish_snapshots()
    await stage(service, job_id, 'billing')
    billing = await api.billing()
    entry = next((b for b in billing.get('billingNodes', []) if
                 b.get('nodeUuid') == node.panel_uuid or (b.get('node') or {}).get('uuid') == node.panel_uuid), None)
    if entry:
        node.billing_uuid = entry['uuid']
        await api.call('PATCH', '/infra-billing/nodes', {'uuids': [entry['uuid']], 'nextBillingAt': deployment.paid_until.isoformat()})
    else:
        await api.call('POST', '/infra-billing/nodes', {'providerUuid': node.provider_uuid,
            'nodeUuid': node.panel_uuid, 'name': node.name, 'nextBillingAt': deployment.paid_until.isoformat()})
        billing = await api.billing()
        entry = next((b for b in billing.get('billingNodes', []) if
                     b.get('nodeUuid') == node.panel_uuid or (b.get('node') or {}).get('uuid') == node.panel_uuid), None)
        if not entry:
            raise RuntimeError('Не найдена созданная запись Infra Billing')
        node.billing_uuid = entry['uuid']
    async with service.factory() as session:
        row = await session.get(Node, node.id)
        row.billing_uuid = node.billing_uuid
        await service.restore_host_access(session, row)
        previous = (await session.execute(select(Deployment).where(Deployment.node_id == node.id,
            Deployment.state == 'active', Deployment.id != deployment.id))).scalars().all()
        for item in previous:
            item.state = 'replaced'
        (await session.get(Deployment, deployment.id)).state = 'active'
        await session.commit()
    await service.publish_snapshots()


async def run_job(service, job_id):
    async with service.factory() as session:
        job = await session.get(Job, job_id)
        node = await session.get(Node, job.node_id)
        deployment = await session.get(Deployment, job.deployment_id)
        config = await read_config(session)
    try:
        if job.kind == 'provision':
            await provision(service, node, deployment, config, job_id)
        elif job.kind == 'keys':
            await stage(service, job_id, 'ssh_keys')
            await ansible(service, deployment, config, kind='keys')
        elif job.kind == 'upgrade':
            await stage(service, job_id, 'ansible')
            deployment.node_version = config[f'node_version_{await service.panel.api_major()}']
            await ansible(service, deployment, config, secret=await service.api.node_secret())
            from .diagnostics import quick_check
            await stage(service, job_id, 'verification')
            await quick_check(service, deployment, node)
            async with service.factory() as changed:
                (await changed.get(Deployment, deployment.id)).node_version = deployment.node_version
                await changed.commit()
        elif job.kind == 'restart':
            await service.api.call('POST', f'/nodes/{node.panel_uuid}/actions/restart')
        elif job.kind in {'small', 'medium', 'large'}:
            from .diagnostics import run_diagnostics
            await run_diagnostics(service, job, node, deployment, config)
        else:
            raise ValueError('Неизвестное задание')
        status, result = 'done', 'Готово'
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Shell/SSH exceptions can echo secrets; only our own safe errors appear.
        status = 'failed'
        result = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
    async with service.factory() as session:
        row = await session.get(Job, job_id)
        row.status, row.result, row.finished_at = status, result[:500], datetime.now(timezone.utc)
        await session.commit()
    try:
        await service.bot.send_message(job.requested_by, f'Задание #{job_id}: {status}\n{result[:500]}')
    except Exception:
        pass
