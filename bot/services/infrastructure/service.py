import asyncio
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import select, text, func
from aiogram.types import FSInputFile

from db.models import (InfrastructureNode as Node, InfrastructureDeployment as Deployment,
                       InfrastructureJob as Job, InfrastructureReport as Report)
from db.dal import app_settings_dal
from bot.services.panel_infrastructure_api import PanelInfrastructureApi
from .settings import read_config, choose_sni, validate_address, next_node_number


def utcnow():
    return datetime.now(timezone.utc)


def country_flag(code):
    return ''.join(chr(127397 + ord(c)) for c in code.upper())


class InfrastructureService:
    def __init__(self, settings, panel, bot, session_factory):
        self.settings, self.panel, self.bot = settings, panel, bot
        self.api = PanelInfrastructureApi(panel)
        self.factory = session_factory
        self.scope = panel.identities.scope
        self.task = None
        self.current = None
        self._last_billing_check = 0
        self._snapshot_lock = asyncio.Lock()

    def cipher(self):
        key = self.settings.INFRA_ENCRYPTION_KEY
        if not key:
            raise ValueError('Не настроен ключ хранения доступов INFRA_ENCRYPTION_KEY')
        return Fernet(key.encode())

    async def nodes(self, session):
        return (await session.execute(select(Node).where(
            Node.panel_scope == self.scope, Node.deleted.is_(False)).order_by(Node.id))).scalars().all()

    async def sync_removed_nodes(self, session, panel_nodes=None):
        """Archive confirmed panel deletions; retain access and report records."""
        panel_nodes = await self.api.nodes() if panel_nodes is None else panel_nodes
        if not isinstance(panel_nodes, list) or any(
                not isinstance(item, dict) or not item.get('uuid') for item in panel_nodes):
            raise RuntimeError('Не удалось получить полный список нод панели')
        present = {item['uuid'] for item in panel_nodes}
        removed = []
        # Installations not yet registered in the panel are not deletions.
        missing = [node for node in await self.nodes(session)
            if node.panel_uuid and node.panel_uuid not in present]
        if not missing:
            return panel_nodes, removed
        billing = await self.api.billing()
        entries = billing.get('billingNodes') if isinstance(billing, dict) else None
        if not isinstance(entries, list) or any(not isinstance(e, dict) or not e.get('uuid') for e in entries):
            raise RuntimeError('Не удалось получить полный список биллинга панели')
        for node in missing:
            # Node deletion can cascade to billing. Some panel versions return
            # 500 A138 instead of 404 when deleting that already missing record.
            linked = lambda entry: entry.get('nodeUuid') or (entry.get('node') or {}).get('uuid')
            entry = next((e for e in entries if linked(e) == node.panel_uuid), None)
            if not entry and node.billing_uuid:
                entry = next((e for e in entries if e['uuid'] == node.billing_uuid
                    and linked(e) is None), None)
            billing_uuid = entry['uuid'] if entry else None
            if billing_uuid:
                result = await self.panel._request('DELETE', f'/infra-billing/nodes/{billing_uuid}')
                if not result or (result.get('error') and result.get('status_code') != 404):
                    raise RuntimeError('Не удалось удалить биллинг удалённой ноды; синхронизация будет повторена')
            node.billing_uuid = None
            node.deleted = True
            jobs = (await session.execute(select(Job).where(Job.node_id == node.id,
                Job.status.in_(['pending', 'running'])))).scalars().all()
            for job in jobs:
                if job.status == 'pending':
                    job.status, job.finished_at = 'cancelled', utcnow()
                    job.result = 'Нода удалена из панели'
                else:
                    job.cancel_requested = True
            removed.append(node.id)
        await session.flush()
        return panel_nodes, removed

    async def node(self, session, node_id, include_deleted=False):
        node = await session.get(Node, int(node_id))
        if not node or node.panel_scope != self.scope or (node.deleted and not include_deleted):
            raise ValueError('Нода не найдена')
        return node

    async def deployment(self, session, node_id):
        return (await session.execute(select(Deployment).where(Deployment.node_id == node_id,
            Deployment.state == 'active').order_by(Deployment.id.desc()).limit(1))).scalar_one_or_none()

    async def enqueue(self, session, node, deployment, kind, admin_id, host_uuid=None):
        if kind == 'provision' and deployment.state in {'replaced', 'superseded'}:
            raise ValueError('Этот VPS уже заменён. Используйте текущую карточку ноды.')
        # Serialize enqueues with replacement: measurements may wait, but a
        # pending installation must not supersede the VPS being measured.
        await session.execute(select(Node.id).where(Node.id == node.id).with_for_update())
        active = (await session.execute(select(Job.kind).where(Job.node_id == node.id,
            Job.status.in_(['pending', 'running'])))).scalars().all()
        if kind.startswith('gemini_') and any(item.startswith('gemini_') for item in active):
            raise ValueError('Настройка Gemini для этой ноды уже ожидает выполнения')
        if active and (kind == 'provision' or 'provision' in active):
            raise ValueError('Дождитесь завершения установки ноды перед новым заданием')
        job = Job(node_id=node.id, deployment_id=deployment.id, kind=kind, requested_by=admin_id)
        session.add(job)
        await session.flush()
        if kind.startswith('gemini_') and host_uuid:
            await app_settings_dal.upsert_setting(session,
                f'infra.gemini.job.{self.scope}.{job.id}', host_uuid, updated_by=admin_id)
        return job

    async def schedule_key_updates(self, session, admin_id):
        """Persist propagation requests, including nodes busy with another job."""
        await session.execute(text('SELECT pg_advisory_xact_lock(74120303)'))
        waiting = []
        nodes = await self.nodes(session)
        for node in nodes:
            await app_settings_dal.upsert_setting(session,
                f'infra.keys.{self.scope}.{node.id}', str(admin_id))
            if not await self.deployment(session, node.id):
                waiting.append(node.name)
        return len(nodes), waiting

    async def enqueue_pending_keys(self):
        async with self.factory() as session:
            await session.execute(text('SELECT pg_advisory_xact_lock(74120303)'))
            for node in await self.nodes(session):
                marker = await app_settings_dal.get_setting(session, f'infra.keys.{self.scope}.{node.id}')
                if not marker or marker.value == '0':
                    continue
                deployment = await self.deployment(session, node.id)
                busy = (await session.execute(select(Job.id).where(Job.node_id == node.id,
                    Job.status.in_(['pending', 'running'])))).first()
                if deployment and not busy:
                    await self.enqueue(session, node, deployment, 'keys', int(marker.value))
                    marker.value = '0'
            await session.commit()

    async def prepare(self, session, values, admin_id, replace_id=None):
        # The DB lock serializes allocation across bot instances as well as FSMs.
        await session.execute(text('SELECT pg_advisory_xact_lock(74120301)'))
        config = dict(await read_config(session))
        for key in ('profile_uuid', 'inbound_uuid'):
            if values.get(key):
                config[key] = values[key]
        if not config['profile_uuid'] or not config['inbound_uuid'] or not config['panel_ip']:
            raise ValueError('Сначала выберите профиль/inbound и IP панели в настройках инфраструктуры')
        inbounds = await self.panel.get_inbounds_by_profile_uuid(config['profile_uuid'], refresh=True)
        if not any(i['uuid'] == config['inbound_uuid'] and str(i.get('port')) == '443'
                   and i.get('type', '').lower() == 'vless' for i in inbounds or []):
            raise ValueError('Выбранный inbound должен быть VLESS на порту 443')
        validate_address(values['address'])
        if not 1 <= int(values['period_days']) <= 3650:
            raise ValueError('Срок: от 1 до 3650 дней')
        if not re.fullmatch(r'[A-Z]{2}', values['country']):
            raise ValueError('Страна: двухбуквенный код')
        duplicates = (await session.execute(select(Deployment.node_id).join(Node, Node.id == Deployment.node_id).where(
            Node.panel_scope == self.scope, Node.deleted.is_(False),
            Deployment.address == values['address'], Deployment.state.in_(['active', 'pending'])))).scalars().all()
        if any(node_id != replace_id for node_id in duplicates):
            raise ValueError('Этот адрес уже используется другой нодой или заданием')
        if replace_id:
            current_node = await self.node(session, replace_id)
            await self.refresh_node(session, current_node)
        selected_provider = current_node.provider_uuid if replace_id else values['provider_uuid']
        if not any(p['uuid'] == selected_provider for p in await self.api.providers()):
            raise ValueError('Провайдер ноды отсутствует в Infra Billing')
        all_panel = await self.api.nodes()
        if replace_id:
            node = await self.node(session, replace_id)
            await session.execute(select(Node.id).where(Node.id == node.id).with_for_update())
            if (await session.execute(select(Job.id).where(Job.node_id == node.id,
                    Job.status.in_(['pending', 'running'])))).first():
                raise ValueError('Дождитесь завершения заданий перед заменой VPS')
            node.period_days = int(values['period_days'])
            stale = (await session.execute(select(Deployment).where(
                Deployment.node_id == node.id, Deployment.state == 'pending'))).scalars().all()
            for item in stale:
                item.state = 'superseded'
        else:
            names = [n['name'] for n in all_panel] + [n.name for n in await self.nodes(session)]
            number = next_node_number(names)
            node = Node(panel_scope=self.scope, name=f"node{number} ({values['country']})",
                        country=values['country'], provider_uuid=values['provider_uuid'],
                        period_days=int(values['period_days']))
            session.add(node)
            await session.flush()
        deployments = (await session.execute(select(Deployment).join(Node, Node.id == Deployment.node_id).where(
            Node.panel_scope == self.scope, Node.deleted.is_(False), Deployment.state.in_(['active', 'pending'])))).scalars().all()
        pool = config['sni_ru'] if node.country == 'RU' else config['sni_other']
        deployment = Deployment(node_id=node.id, address=values['address'],
            host_address=values.get('host_address') or values['address'], panel_address=values['address'],
            ssh_port=int(values.get('ssh_port', 22)), ssh_user=values.get('ssh_user', 'root'),
            credentials=self.cipher().encrypt(values['password'].encode()).decode(),
            host_public_key=values['host_public_key'],
            profile_uuid=config['profile_uuid'], inbound_uuid=config['inbound_uuid'],
            sni=choose_sni(pool, deployments),
            node_version=config[f'node_version_{await self.panel.api_major()}'],
            paid_until=utcnow() + timedelta(days=int(values['period_days'])))
        session.add(deployment)
        await session.flush()
        job = await self.enqueue(session, node, deployment, 'provision', admin_id)
        return node, job

    async def adopt(self, session, panel_uuid):
        """Import a panel node without guessing host association or SSH access."""
        await session.execute(text('SELECT pg_advisory_xact_lock(74120301)'))
        node_data = next((n for n in await self.api.nodes() if n['uuid'] == panel_uuid), None)
        if not node_data:
            raise ValueError('Нода панели не найдена')
        hosts = await self.panel.get_all_hosts()
        if hosts is None:
            raise RuntimeError('Не удалось получить hosts')
        candidates = [h for h in hosts if h.get('nodes') == [panel_uuid]]
        node = Node(panel_scope=self.scope, panel_uuid=panel_uuid, host_automation=False,
                    host_uuid=candidates[0]['uuid'] if len(candidates) == 1 else None,
                    name=node_data['name'], country=node_data['countryCode'],
                    provider_uuid=node_data.get('providerUuid') or '', period_days=30)
        session.add(node)
        await session.flush()
        billing = await self.api.billing()
        entry = next((b for b in billing.get('billingNodes', []) if (b.get('nodeUuid') == panel_uuid or
                     (b.get('node') or {}).get('uuid') == panel_uuid)), None)
        if entry:
            node.billing_uuid = entry['uuid']
            if not node.provider_uuid:
                node.provider_uuid = entry['providerUuid']
        return node

    async def refresh_node(self, session, node):
        """Read mutable panel metadata without silently moving stored SSH access."""
        if not node.panel_uuid:
            return
        current = next((n for n in await self.api.nodes() if n['uuid'] == node.panel_uuid), None)
        if not current:
            raise ValueError('Нода удалена из панели. Доступы и история сохранены в боте.')
        hosts = await self.panel.get_all_hosts()
        if hosts is None:
            raise RuntimeError('Не удалось получить hosts')
        node.name = current.get('name') or node.name
        node.country = current.get('countryCode') or node.country
        node.provider_uuid = current.get('providerUuid') or ''
        candidates = [h for h in hosts if h.get('nodes') == [node.panel_uuid]]
        host = next((h for h in candidates if h['uuid'] == node.host_uuid), None)
        if not host:
            host = candidates[0] if len(candidates) == 1 else None
        node.host_uuid = host['uuid'] if host else None
        entry = await self.billing_entry(session, node)
        deployment = await self.deployment(session, node.id)
        if deployment:
            deployment.paid_until = (datetime.fromisoformat(entry['nextBillingAt'].replace('Z', '+00:00'))
                if entry and entry.get('nextBillingAt') else None)
        if deployment and host:
            deployment.host_address = host.get('address') or deployment.host_address
            deployment.sni = host.get('sni') or None
            inbound = host.get('inbound') or {}
            cp = current.get('configProfile') or {}
            active = [i['uuid'] if isinstance(i, dict) else i for i in cp.get('activeInbounds', [])]
            if cp.get('activeConfigProfileUuid') and len(active) == 1:
                deployment.profile_uuid = cp['activeConfigProfileUuid']
                deployment.inbound_uuid = active[0]
            elif inbound.get('configProfileUuid') and inbound.get('configProfileInboundUuid'):
                deployment.profile_uuid = inbound['configProfileUuid']
                deployment.inbound_uuid = inbound['configProfileInboundUuid']
            version = (current.get('versions') or {}).get('node')
            if version:
                deployment.node_version = version
        # A manual node-address change may mean a different physical server.
        # Credentials/host-key cannot be reused safely just because metadata changed.
        return current

    async def save_access(self, session, node, address, port, user, password):
        """Verify and store SSH credentials; panel settings and VPS stay untouched."""
        import asyncssh
        from .provisioning import host_key
        address = validate_address(address)
        if not 1 <= port <= 65535 or user != 'root' or not password or '\n' in password:
            raise ValueError('Проверьте порт, root-логин и пароль')
        await session.execute(select(Node.id).where(Node.id == node.id).with_for_update())
        if (await session.execute(select(Job.id).where(Job.node_id == node.id,
                Job.status.in_(['pending', 'running'])))).first():
            raise ValueError('Дождитесь завершения заданий перед изменением SSH-доступа')
        current = await self.refresh_node(session, node)
        if not current:
            raise ValueError('Нода панели не найдена')
        public, fingerprint = await host_key(address, port)
        async with asyncssh.connect(host=address, port=port, username=user, password=password,
                client_keys=[], known_hosts=([asyncssh.import_public_key(public)], [], []),
                connect_timeout=20, login_timeout=20):
            pass
        previous = await self.deployment(session, node.id)
        cp = current.get('configProfile') or {}
        inbounds = cp.get('activeInbounds') or []
        inbound = inbounds[0] if len(inbounds) == 1 else None
        if isinstance(inbound, dict):
            inbound = inbound['uuid']
        entry = await self.billing_entry(session, node)
        new_identity = (not previous or previous.address != address or previous.host_public_key != public
            or (previous.panel_address and previous.panel_address != current['address']))
        if new_identity:
            if previous:
                previous.state = 'replaced'
            deployment = Deployment(node_id=node.id, state='active', address=address,
                host_address=previous.host_address if previous else current['address'],
                profile_uuid=cp.get('activeConfigProfileUuid'), inbound_uuid=inbound,
                node_version=(current.get('versions') or {}).get('node') or 'unknown',
                paid_until=datetime.fromisoformat(entry['nextBillingAt'].replace('Z', '+00:00')) if entry else None)
            session.add(deployment)
        else:
            deployment = previous
        deployment.ssh_port, deployment.ssh_user = port, user
        deployment.credentials = self.cipher().encrypt(password.encode()).decode()
        deployment.host_public_key = public
        deployment.panel_address = current['address']
        # Read the unique host address if present, but never create or patch hosts.
        if node.host_uuid:
            hosts = await self.panel.get_all_hosts()
            host = next((h for h in hosts or [] if h['uuid'] == node.host_uuid), None)
            if host:
                deployment.host_address = host['address']
        await session.flush()
        return deployment, fingerprint

    async def validate_deployment(self, session, node, deployment):
        current = await self.refresh_node(session, node)
        if current and validate_address(current['address']) != validate_address(getattr(deployment, 'panel_address', None) or deployment.address):
            raise ValueError('IP ноды изменён в панели. Сохраните SSH-доступ нового сервера перед запуском задания.')

    async def billing_entry(self, session, node, billing=None):
        billing = billing if billing is not None else await self.api.billing()
        entries = (billing or {}).get('billingNodes', [])

        def linked_node(entry):
            return entry.get('nodeUuid') or (entry.get('node') or {}).get('uuid')

        entry = next((e for e in entries if node.billing_uuid and node.panel_uuid
                      and e['uuid'] == node.billing_uuid and linked_node(e) == node.panel_uuid), None)
        if not entry and node.panel_uuid:
            entry = next((e for e in entries if linked_node(e) == node.panel_uuid), None)
        if entry:
            node.billing_uuid = entry['uuid']
            provider = entry.get('providerUuid') or (entry.get('provider') or {}).get('uuid')
            if provider:
                node.provider_uuid = provider
        else:
            node.billing_uuid = None
        return entry

    async def extend(self, session, node, days, amount=None):
        if amount is not None and (not 0 <= amount <= 100000000):
            raise ValueError('Некорректная сумма')
        if not 1 <= days <= 3650:
            raise ValueError('Срок: от 1 до 3650 дней')
        entry = await self.billing_entry(session, node)
        if not entry:
            raise ValueError('Подключите ноду к Infra Billing в панели и укажите срок оплаты')
        old = datetime.fromisoformat(entry['nextBillingAt'].replace('Z', '+00:00'))
        deadline = max(old, utcnow()) + timedelta(days=days)
        await self.api.call('PATCH', '/infra-billing/nodes', {'uuids': [node.billing_uuid], 'nextBillingAt': deadline.isoformat()})
        node.period_days = days
        deployment = await self.deployment(session, node.id)
        if deployment:
            deployment.paid_until = deadline
        if amount is not None:
            if amount < 0:
                raise ValueError('Стоимость не может быть отрицательной')
            await self.api.call('POST', '/infra-billing/history', {'providerUuid': node.provider_uuid,
                'amount': amount, 'billedAt': utcnow().isoformat()})
        await self.restore_host_access(session, node)
        return deadline

    async def tg_host_access(self, session, node, host_uuid=None, toggle=False):
        config = await read_config(session)
        squad = config['tg_squad_uuid']
        if not squad:
            raise ValueError('Выберите squad tg в настройках инфраструктуры')
        hosts = await self.panel.get_all_hosts()
        if hosts is None:
            raise RuntimeError('Cannot fetch hosts')
        candidates = [h for h in hosts if node.panel_uuid in [
            item.get('uuid') if isinstance(item, dict) else item for item in h.get('nodes', [])]]
        selected = host_uuid or node.host_uuid
        host = next((h for h in candidates if h['uuid'] == selected), None)
        if host_uuid and host is None:
            raise ValueError('У ноды нет связанных hosts')
        if host is None and len(candidates) == 1:
            host = candidates[0]
        if host is None:
            return None, candidates
        major = await self.panel.api_major()
        if major == 2:
            excluded = list(host.get('excludedInternalSquads') or [])
            visible = squad not in excluded
            values = {'excludedInternalSquads': [x for x in excluded if x != squad]
                if not visible else list(dict.fromkeys(excluded + [squad]))}
        else:
            access = dict(host.get('internalSquads') or {'mode': 'EXCLUDE', 'squads': []})
            squads = list(access.get('squads') or [])
            if access['mode'] not in {'ALLOW_ONLY', 'EXCLUDE'}:
                raise ValueError('Unsupported host access mode')
            visible = (squad in squads) if access['mode'] == 'ALLOW_ONLY' else (squad not in squads)
            add = (not visible) if access['mode'] == 'ALLOW_ONLY' else visible
            access['squads'] = list(dict.fromkeys(squads + [squad])) if add else [x for x in squads if x != squad]
            # ALLOW_ONLY with an empty list means no squads; never disable the whole host.
            values = {'internalSquads': access}
        if toggle:
            await self.api.call('PATCH', '/hosts', dict(values, uuid=host['uuid']))
            if host['uuid'] == node.host_uuid:
                node.excluded_tg = visible
                node.previous_host_access = None
            visible = not visible
        return visible, candidates

    async def restore_host_access(self, session, node):
        if node.excluded_tg and node.host_uuid and node.previous_host_access:
            hosts = await self.panel.get_all_hosts()
            host = next((h for h in hosts or [] if h['uuid'] == node.host_uuid), None)
            if host:
                saved = json.loads(node.previous_host_access)
                major = await self.panel.api_major()
                previous = self.api.access_for_version(saved.get('before', saved), major)
                expected = saved.get('after')
                if expected is not None:
                    expected = self.api.access_for_version(expected, major)
                if expected is None or all(host.get(key) == value for key, value in expected.items()):
                    await self.api.call('PATCH', '/hosts', dict(previous, uuid=node.host_uuid))
                node.excluded_tg = False
                node.previous_host_access = None

    async def delete(self, session, node):
        active = (await session.execute(select(Job.id).where(Job.node_id == node.id,
                  Job.status.in_(['pending', 'running'])))).first()
        if active:
            raise ValueError('Сначала остановите активное задание')
        for path in ([f'/hosts/{node.host_uuid}'] if node.host_uuid else []) + (
                     [f'/infra-billing/nodes/{node.billing_uuid}'] if node.billing_uuid else []) + (
                     [f'/nodes/{node.panel_uuid}'] if node.panel_uuid else []):
            result = await self.panel._request('DELETE', path)
            if not result or (result.get('error') and result.get('status_code') != 404):
                raise RuntimeError('Не удалось удалить один из объектов; повторите операцию')
        node.deleted = True  # keep deployment/test history

    async def snapshot(self, session, node_id=None, admin_id=None):
        nodes = [await self.node(session, node_id, include_deleted=True)] if node_id else await self.nodes(session)
        sections = []
        for node in nodes:
            deployment = await self.deployment(session, node.id)
            if not deployment:
                deployment = (await session.execute(select(Deployment).where(
                    Deployment.node_id == node.id, Deployment.state.notin_(['replaced', 'superseded']))
                    .order_by(Deployment.id.desc()).limit(1))).scalar_one_or_none()
            if not deployment:
                continue
            password = self.cipher().decrypt(deployment.credentials.encode()).decode()
            command_host = f'[{deployment.address}]' if ':' in deployment.address else deployment.address
            command = f'ssh {deployment.ssh_user}@{command_host}'
            if deployment.ssh_port != 22:
                command += f' -p {deployment.ssh_port}'
            lines = [f'{country_flag(node.country)} {html.escape(node.name)}']
            lines += [f'<code>{html.escape(v)}</code>' for v in [deployment.address, password, command]]
            sections.append('\n'.join(lines))
        messages, current = [], ''
        for section in sections:
            if len(current) + len(section) > 3500:
                messages.append(current); current = ''
            current += ('\n\n' if current else '') + section
        if current:
            messages.append(current)
        if messages:
            return messages
        from .localization import admin_translator
        translate = await admin_translator(self, session, admin_id)
        return [translate('infra_no_saved_access')]

    async def publish_snapshots(self, all_admins=True, admin_id=None, force_new=False):
        async with self._snapshot_lock:
            for recipient in (self.settings.ADMIN_IDS if all_admins else [admin_id]):
                try:
                    async with self.factory() as session:
                        texts = await self.snapshot(session, admin_id=recipient)
                        from .localization import admin_translator
                        translate = await admin_translator(self, session, recipient)
                        key = f'infra.snapshot.{recipient}.{self.scope}'
                        row = await app_settings_dal.get_setting(session, key)
                        previous = json.loads(row.value) if row else []
                        ids = []
                        for index, content in enumerate(texts):
                            old_id = previous[index] if not force_new and index < len(previous) else None
                            if old_id:
                                try:
                                    await self.bot.edit_message_text(content, chat_id=recipient, message_id=old_id, parse_mode='HTML')
                                    ids.append(old_id); continue
                                except Exception as exc:
                                    if 'message is not modified' in str(exc):
                                        ids.append(old_id); continue
                            sent = await self.bot.send_message(recipient, content, parse_mode='HTML')
                            ids.append(sent.message_id)
                        for old_id in previous[len(texts):]:
                            try:
                                await self.bot.edit_message_text(translate('infra_snapshot_replaced'),
                                    chat_id=recipient, message_id=old_id)
                            except Exception:
                                pass
                        await app_settings_dal.upsert_setting(session, key, json.dumps(ids))
                        await session.commit()
                except Exception:
                    logging.warning('Cannot deliver infrastructure credentials to admin %s', recipient)
                    if force_new:
                        raise RuntimeError('Не удалось отправить SSH-доступы. Проверьте сохранённые доступы и ключ шифрования.') from None

    async def start(self):
        if not self.settings.INFRA_SSH_PRIVATE_KEY:
            import asyncssh
            directory = Path(self.settings.INFRA_ARTIFACT_DIR)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / 'automation_key'
            if not path.exists():
                with path.open('xb') as stream:
                    path.chmod(0o600)
                    stream.write(asyncssh.generate_private_key('ssh-ed25519').export_private_key())
            self.settings.INFRA_SSH_PRIVATE_KEY = str(path)
        self.cipher()  # fail before accepting any secret-bearing workflow
        self.task = asyncio.create_task(self.worker(), name='infrastructure-worker')

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def worker(self):
        # Session-level advisory lock elects one worker for all bot instances.
        from .provisioning import run_job
        from .job_messages import publish_job, refresh_queue_messages
        while True:
            try:
                async with self.factory() as lease:
                    locked = (await lease.execute(text('SELECT pg_try_advisory_lock(74120302)'))).scalar()
                    if not locked:
                        await asyncio.sleep(10); continue
                    try:
                        while True:
                            await refresh_queue_messages(self)
                            # Preserve queued jobs while the panel starts or is unavailable.
                            await self.api.call('GET', '/system/metadata')
                            await self.enqueue_pending_keys()
                            async with self.factory() as session:
                                job = (await session.execute(select(Job).join(Node, Node.id == Job.node_id).where(
                                    Node.panel_scope == self.scope, Job.status.in_(['pending', 'running'])).order_by(Job.id).limit(1))).scalar_one_or_none()
                                if job:
                                    if job.status == 'running':
                                        job.status, job.result = 'interrupted', 'Задание прервано перезапуском. Можно повторить.'
                                        job.finished_at = utcnow()
                                        await session.commit()
                                        await publish_job(self, job.id, complete=True)
                                    else:
                                        job.status = 'running'
                                        job.started_at = utcnow()
                                        await session.commit()
                                        await publish_job(self, job.id)
                                        self.current = asyncio.create_task(run_job(self, job.id))
                                        try:
                                            message_tick = 0
                                            while not self.current.done():
                                                await asyncio.sleep(2)
                                                message_tick += 1
                                                if message_tick % 3 == 0:
                                                    await refresh_queue_messages(self)
                                                try:
                                                    await self.check_billing()
                                                except Exception:
                                                    logging.exception('Infrastructure billing check failed')
                                                async with self.factory() as cancel_session:
                                                    row = await cancel_session.get(Job, job.id)
                                                    if row.cancel_requested and row.status == 'running':
                                                        self.current.cancel()
                                            await self.current
                                        except asyncio.CancelledError:
                                            async with self.factory() as stopped:
                                                row = await stopped.get(Job, job.id)
                                                if row.status in {'pending', 'running'}:
                                                    row.status = 'interrupted' if asyncio.current_task().cancelling() else 'cancelled'
                                                    row.finished_at = utcnow()
                                                    await stopped.commit()
                                            await publish_job(self, job.id, complete=True)
                                            if asyncio.current_task().cancelling():
                                                raise
                                        finally:
                                            if self.current and not self.current.done():
                                                self.current.cancel()
                                                await asyncio.gather(self.current, return_exceptions=True)
                                            self.current = None
                                else:
                                    await self.check_billing()
                            await asyncio.sleep(5)
                    finally:
                        await lease.execute(text('SELECT pg_advisory_unlock(74120302)'))
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Infrastructure worker iteration failed')
                await asyncio.sleep(15)

    async def check_billing(self):
        # Re-read panel deadlines so web-UI changes are authoritative.
        import time
        if time.monotonic() - self._last_billing_check < 60:
            return
        self._last_billing_check = time.monotonic()
        async with self.factory() as session:
            await self.sync_removed_nodes(session)
            await session.commit()
            config = await read_config(session)
            billing = await self.api.billing()
            providers = {p['uuid']: p['name'] for p in await self.api.providers()}
            for node in await self.nodes(session):
                entry = await self.billing_entry(session, node, billing)
                if not entry:
                    continue
                deadline = datetime.fromisoformat(entry['nextBillingAt'].replace('Z', '+00:00'))
                hours = (deadline - utcnow()).total_seconds() / 3600
                eligible = [threshold for threshold in config['warning_hours'] if hours <= threshold]
                for threshold in [min(eligible)] if eligible else []:
                    for admin in self.settings.ADMIN_IDS:
                        preference = await app_settings_dal.get_setting(session, f'infra.notify.{admin}')
                        if preference and preference.value == 'false':
                            continue
                        marker = f'infra.warn.{node.id}.{threshold}.{admin}'
                        sent = await app_settings_dal.get_setting(session, marker)
                        if sent and sent.value == deadline.isoformat():
                            continue
                        from .localization import admin_translator
                        translate = await admin_translator(self, session, admin)
                        timing = translate('infra_billing_overdue') if hours < 0 else translate('infra_billing_remaining', p0=max(0, int(hours)))
                        message = translate('infra_billing_reminder', p0=html.escape(node.name), p1=timing,
                            p2=html.escape(providers.get(node.provider_uuid, '—')), p3=deadline.strftime('%d.%m.%Y %H:%M UTC'))
                        try:
                            from aiogram.utils.keyboard import InlineKeyboardBuilder
                            kb = InlineKeyboardBuilder()
                            kb.button(text=translate('infra_open_node'), callback_data=f'infra:node:{node.id}')
                            await self.bot.send_message(admin, message, reply_markup=kb.as_markup(), parse_mode='HTML')
                            await app_settings_dal.upsert_setting(session, marker, deadline.isoformat())
                        except Exception:
                            logging.warning('Cannot deliver billing reminder to %s', admin)
                if hours <= 0 and config['expire_exclude_tg'] and node.host_automation and not node.excluded_tg:
                    if not config['tg_squad_uuid'] or not node.host_uuid:
                        continue
                    hosts = await self.panel.get_all_hosts()
                    host = next((h for h in hosts or [] if h['uuid'] == node.host_uuid), None)
                    if host:
                        # Commit original state BEFORE panel mutation for restart recovery.
                        if node.previous_host_access:
                            saved = json.loads(node.previous_host_access)
                        else:
                            before, after = self.api.exclusion_values(host, config['tg_squad_uuid'], await self.panel.api_major())
                            saved = {'before': before, 'after': after}
                            node.previous_host_access = json.dumps(saved)
                            await session.commit()
                        await self.api.call('PATCH', '/hosts', dict(saved['after'], uuid=node.host_uuid))
                        node.excluded_tg = True
            try:
                from .gemini import detect_panel_selected_gemini
                gemini_jobs = await detect_panel_selected_gemini(self, session, await self.nodes(session))
            except Exception:
                logging.exception('Gemini inbound synchronization check failed')
                gemini_jobs = []
            await session.commit()
        if gemini_jobs:
            from .job_messages import publish_job
            for job_id in gemini_jobs:
                await publish_job(self, job_id)
