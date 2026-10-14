"""haproxy SNI fronting bridges on the shared bridge server."""
import copy
import json

from db.models import InfrastructureDeployment as Deployment
from .gemini import (parse_config, read_profile, switch_profile, _panel_node_host,
    linked_hosts, active_inbound_ids, find_inbound, create_or_reuse_gemini_inbound,
    add_inbound_to_squads, restore_squads, check_other_hosts, assert_selection_unchanged,
    verify_connected, save_host_group_state)
from .settings import validate_hostname

WILDCARD_TAG = 'VLESS-WILDCARD'


def bridge_host_remark(node_name):
    return f'{node_name} bridge'


def _host_nodes(host):
    return [item.get('uuid') if isinstance(item, dict) else item for item in host.get('nodes', [])]


def _wildcard_uuid(profile):
    for item in profile.get('inbounds') or []:
        if item.get('tag') == WILDCARD_TAG:
            return item['uuid']
    raise ValueError(f'В профиле панели нет inbound {WILDCARD_TAG}')


def _reality_by_tag(config, tag):
    for entry in config.get('inbounds') or []:
        if entry.get('tag') == tag:
            reality = (entry.get('streamSettings') or {}).get('realitySettings')
            if not reality:
                raise ValueError(f'Inbound {tag} не содержит REALITY настроек')
            return reality
    raise ValueError(f'Inbound {tag} не найден в конфигурации профиля')


async def _wildcard_state(api, profile_uuid):
    profile = await read_profile(api, profile_uuid)
    config = parse_config(profile.get('config'))
    return profile, config, _reality_by_tag(config, WILDCARD_TAG)


def wildcard_variants(config):
    return [entry for entry in config.get('inbounds') or []
        if entry.get('tag', '').casefold() in {WILDCARD_TAG.casefold(), (WILDCARD_TAG + '-GEMINI').casefold()}]


async def add_profile_sni(api, profile_uuid, sni):
    """Keep a wildcard and its Gemini sibling consistent without touching other inbounds."""
    profile, config, reality = await _wildcard_state(api, profile_uuid)
    changed = False
    for entry in wildcard_variants(config):
        names = list(entry['streamSettings']['realitySettings'].get('serverNames') or [])
        if sni not in names:
            entry['streamSettings']['realitySettings']['serverNames'] = names + [sni]
            changed = True
    if not changed:
        return False
    await api.call('PATCH', '/config-profiles', {'uuid': profile_uuid, 'config': config})
    _, checked, _ = await _wildcard_state(api, profile_uuid)
    if any(sni not in entry['streamSettings']['realitySettings'].get('serverNames', []) for entry in wildcard_variants(checked)):
        raise RuntimeError('Панель не подтвердила добавление SNI в профиль')
    return True


async def remove_profile_sni(api, profile_uuid, sni, preserve_tags=()):
    """Remove only names added by this operation, preserving pre-existing variant names."""
    profile, config, reality = await _wildcard_state(api, profile_uuid)
    changed = False
    for entry in wildcard_variants(config):
        names = list(entry['streamSettings']['realitySettings'].get('serverNames') or [])
        if sni in names and entry['tag'] not in preserve_tags:
            entry['streamSettings']['realitySettings']['serverNames'] = [name for name in names if name != sni]
            changed = True
    if changed:
        await api.call('PATCH', '/config-profiles', {'uuid': profile_uuid, 'config': config})
    return changed


def verify_listener_compatibility(source, target):
    """Changing host bindings must not change its existing REALITY client parameters."""
    source_stream, target_stream = source.get('streamSettings') or {}, target.get('streamSettings') or {}
    source_reality, target_reality = source_stream.get('realitySettings') or {}, target_stream.get('realitySettings') or {}
    if (source.get('port') != 443 or target.get('port') != 443
            or source.get('protocol') != target.get('protocol')
            or source_stream.get('network') != target_stream.get('network')
            or source_stream.get('security') != target_stream.get('security')
            or source_reality.get('privateKey') != target_reality.get('privateKey')
            or set(source_reality.get('shortIds') or []) != set(target_reality.get('shortIds') or [])
            or source_reality.get('dest', source_reality.get('target')) != target_reality.get('dest', target_reality.get('target'))):
        raise ValueError('Wildcard inbound меняет параметры подключения REALITY; согласуйте ключ, shortIds, transport и порт перед созданием моста')


async def create_bridge_host(api, node, bridge_address, sni, profile_uuid, inbound_uuid):
    """Create the panel host pointing at the bridge; returns (uuid, created)."""
    hosts = await api.call('GET', '/hosts') or []
    remark = bridge_host_remark(node.name)
    # One SNI is a single haproxy route: it must not be claimed by any other host.
    existing = next((host for host in hosts if host.get('sni') == sni
        and host.get('remark') == remark and host.get('address') == bridge_address
        and _host_nodes(host) == [node.panel_uuid]), None)
    if existing:
        if existing.get('inbound') != {'configProfileUuid': profile_uuid, 'configProfileInboundUuid': inbound_uuid}:
            raise ValueError('Существующий host моста использует другой inbound; синхронизируйте его в панели')
        return existing['uuid'], False
    duplicate = next((host for host in hosts if host.get('sni') == sni), None)
    if duplicate:
        raise ValueError('Этот SNI уже используется другим host')
    result = await api.call('POST', '/hosts', {'address': bridge_address, 'port': 443,
        'nodes': [node.panel_uuid], 'inbound': {'configProfileUuid': profile_uuid,
        'configProfileInboundUuid': inbound_uuid}, 'sni': sni, 'remark': remark})
    if not (result or {}).get('uuid'):
        raise RuntimeError('Панель не вернула созданный host моста')
    return result['uuid'], True


async def create_bridge(service, node, deployment, config, job_id, sni):
    """Provision one bridge: wildcard profile first, then haproxy, then the host."""
    from .provisioning import ansible_bridge, stage

    if not sni:
        raise ValueError('Задание не содержит SNI моста')
    sni = validate_hostname(sni)
    if not config['bridge_address'] or not config['bridge_host_public_key']:
        raise ValueError('Мост не настроен: укажите адрес моста и проверьте подключение в настройках инфраструктуры')
    if not node.panel_uuid:
        raise ValueError('Нода не подключена к панели')
    api = service.api

    hosts = linked_hosts(await api.call('GET', '/hosts') or [], node.panel_uuid)
    direct_candidates = [host for host in hosts if host.get('address') != config['bridge_address']]
    direct_host = next((host for host in direct_candidates if host['uuid'] == getattr(node, 'host_uuid', None)), None)
    if direct_host is None and len(direct_candidates) == 1:
        direct_host = direct_candidates[0]
    if not direct_host:
        raise ValueError('Не удалось однозначно выбрать прямой host ноды для моста')
    panel_node, direct_host = await _panel_node_host(api, node.panel_uuid, direct_host['uuid'])
    cp = panel_node.get('configProfile') or {}
    wildcard_profile = cp.get('activeConfigProfileUuid')
    previous_inbounds = active_inbound_ids(panel_node)
    if not wildcard_profile or not previous_inbounds:
        raise ValueError('Не удалось определить текущий профиль ноды')
    profile = await read_profile(api, wildcard_profile)
    wildcard_uuid = _wildcard_uuid(profile)
    source_id = (direct_host.get('inbound') or {}).get('configProfileInboundUuid')
    source_tag = next((i['tag'] for i in profile.get('inbounds') or [] if i['uuid'] == source_id), None)
    if source_id not in previous_inbounds or (direct_host.get('inbound') or {}).get('configProfileUuid') != wildcard_profile:
        raise ValueError('Прямой host не использует активный inbound ноды')
    source_config = parse_config(profile.get('config'))
    source_body = find_inbound(source_config, source_tag)
    verify_listener_compatibility(source_body, find_inbound(source_config, WILDCARD_TAG))
    gemini_enabled = 'gemini' in source_tag.casefold()
    accepted_names = list(dict.fromkeys([sni] + list(_reality_by_tag(source_config, source_tag).get('serverNames') or [])
        + ([direct_host['sni']] if direct_host.get('sni') else [])))
    original_names = {entry['tag']: list(entry['streamSettings']['realitySettings'].get('serverNames') or [])
        for entry in wildcard_variants(source_config)}
    previous_host_inbound = copy.deepcopy(direct_host.get('inbound'))
    wildcard_host_inbound = {'configProfileUuid': wildcard_profile,
                             'configProfileInboundUuid': wildcard_uuid}
    hosts = await api.call('GET', '/hosts') or []
    existing_bridge = next((host for host in hosts if host.get('address') == config['bridge_address']
        and node.panel_uuid in _host_nodes(host)), None)
    if existing_bridge and existing_bridge.get('sni') != sni:
        raise ValueError('У этой ноды уже есть мост с другим SNI')
    # A pre-existing bridge (retry after a failed compensation) is left intact:
    # rollback only removes what this run actually created.
    route_existed = existing_bridge is not None
    await check_other_hosts(api, node.panel_uuid, direct_host['uuid'], wildcard_profile, source_id,
        allowed_host_uuids=[existing_bridge['uuid']] if existing_bridge else [])
    target = f'[{deployment.address}]' if ':' in deployment.address else deployment.address
    backend = f'{target}:443'

    # The profile accepts the SNI and the node runs only the wildcard inbound
    # before haproxy starts forwarding the SNI to it.
    attempted_names = []
    changed_squads = []
    node_switched = host_switched = bridge_added = False
    host_uuid = None
    host_created = False
    try:
        await stage(service, job_id, 'bridge_profile')
        for name in accepted_names:
            attempted_names.append(name)
            await add_profile_sni(api, wildcard_profile, name)
        if gemini_enabled:
            profile, _source_tag, _tag, wildcard_uuid = await create_or_reuse_gemini_inbound(api, wildcard_profile, wildcard_uuid)
        wildcard_host_inbound = {'configProfileUuid': wildcard_profile, 'configProfileInboundUuid': wildcard_uuid}
        target_inbounds = list(dict.fromkeys(wildcard_uuid if identifier == source_id else identifier for identifier in previous_inbounds))
        for identifier in target_inbounds:
            if identifier == wildcard_uuid:
                continue
            other_tag = next(i['tag'] for i in profile['inbounds'] if i['uuid'] == identifier)
            if find_inbound(parse_config(profile['config']), other_tag).get('port') == 443:
                raise ValueError('Другой активный inbound уже использует порт моста 443')
        changed_squads = await add_inbound_to_squads(api, source_id, wildcard_uuid)
        await assert_selection_unchanged(api, panel_node, direct_host)
        if previous_inbounds != target_inbounds:
            node_switched = True
            await switch_profile(api, node.panel_uuid, wildcard_profile, target_inbounds)
        if previous_host_inbound != wildcard_host_inbound:
            host_switched = True
            await api.call('PATCH', '/hosts', dict(uuid=direct_host['uuid'],
                inbound=copy.deepcopy(wildcard_host_inbound)))
        await api.restart_node(node.panel_uuid, force=True)
        await verify_connected(api, node.panel_uuid)
        await stage(service, job_id, 'bridge')
        bridge_added = not route_existed
        await ansible_bridge(service, config, sni, backend)
        await stage(service, job_id, 'bridge_host')
        host_uuid, host_created = await create_bridge_host(api, node, config['bridge_address'], sni,
                                                           wildcard_profile, wildcard_uuid)
        hosts = await api.call('GET', '/hosts') or []
        verified = next((host for host in hosts if host.get('uuid') == host_uuid
            and host.get('sni') == sni and not host.get('isDisabled')
            and host.get('inbound') == wildcard_host_inbound
            and host.get('address') == config['bridge_address'] and _host_nodes(host) == [node.panel_uuid]), None)
        if not verified:
            raise RuntimeError('Панель не подтвердила созданный host моста')
        # Register the shared backend listener so Gemini switches both public hosts together.
        async with service.factory() as session:
            row = await session.get(Deployment, deployment.id)
            row.profile_uuid, row.inbound_uuid = wildcard_profile, wildcard_uuid
            from db.dal import app_settings_dal
            await app_settings_dal.upsert_setting(session, f'infra.bridge.{service.scope}.{node.id}', json.dumps({
                'direct_host_uuid': direct_host['uuid'], 'bridge_host_uuid': host_uuid, 'sni': sni}))
            if gemini_enabled:
                base_uuid = _wildcard_uuid(profile)
                state = {'layout': 'inbound', 'enabled': True, 'base_profile_uuid': wildcard_profile,
                    'base_inbound_uuid': base_uuid, 'base_host_inbound': {
                        'configProfileUuid': wildcard_profile, 'configProfileInboundUuid': base_uuid},
                    'gemini_profile_uuid': wildcard_profile, 'gemini_inbound_uuid': wildcard_uuid,
                    'source_tag': WILDCARD_TAG, 'gemini_tag': WILDCARD_TAG + '-GEMINI'}
                await save_host_group_state(service, session, node, [direct_host, {'uuid': host_uuid}], state, None)
            await session.commit()
    except BaseException:
        # Compensate in reverse order; a partial bridge must not stay behind.
        rollback_errors = []
        if host_uuid and host_created:
            try:
                await api.call('DELETE', f'/hosts/{host_uuid}')
            except Exception as exc:
                rollback_errors.append(exc)
        if bridge_added:
            try:
                await ansible_bridge(service, config, sni, backend, state='absent')
            except Exception as exc:
                rollback_errors.append(exc)
        if host_switched and previous_host_inbound:
            try:
                await api.call('PATCH', '/hosts', dict(uuid=direct_host['uuid'],
                    inbound=copy.deepcopy(previous_host_inbound)))
            except Exception as exc:
                rollback_errors.append(exc)
        if node_switched:
            try:
                await switch_profile(api, node.panel_uuid, wildcard_profile, previous_inbounds)
                await api.restart_node(node.panel_uuid, force=True)
                await verify_connected(api, node.panel_uuid)
            except Exception as exc:
                rollback_errors.append(exc)
        try:
            await restore_squads(api, changed_squads, source_id, wildcard_uuid)
        except Exception as exc:
            rollback_errors.append(exc)
        for name in reversed(attempted_names):
            try:
                preserve = [tag for tag, names in original_names.items() if name in names]
                await remove_profile_sni(api, wildcard_profile, name, preserve_tags=preserve)
            except Exception as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            raise RuntimeError('Создание моста не завершилось, автоматический откат требует проверки') from rollback_errors[0]
        raise
