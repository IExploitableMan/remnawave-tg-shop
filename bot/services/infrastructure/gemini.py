"""Gemini-only Psiphon inbound modifiers for Remnawave panels."""
import copy
import json
import re


MODIFIER = 'GEMINI'
SOCKS_TAG = 'psiphon-out'
SOCKS_ADDRESS = '127.0.0.1'
SOCKS_PORT = 1080
BLOCK_TAG = 'BLOCK'
GOOGLE_DOMAINS = [
    'domain:gemini.google.com', 'domain:aistudio.google.com',
    'domain:generativelanguage.googleapis.com',
    'domain:alkalimakersuite-pa.clients6.google.com',
    'domain:alkali-pa.clients6.google.com', 'domain:waa-pa.clients6.google.com',
    'domain:accounts.google.com', 'domain:consent.google.com',
    'domain:google.com', 'domain:googleapis.com', 'domain:gstatic.com',
    'domain:googleusercontent.com', 'domain:googlevideo.com',
    'domain:clients6.google.com', 'domain:ai.google.dev',
    'domain:googleapis.cn', 'domain:googleusercontent.cn',
]


def parse_config(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('Профиль панели не содержит конфигурацию Xray')
    return copy.deepcopy(value)


def modified_inbound_tag(source_tag, modifiers=(MODIFIER,)):
    """Keep the source tag and use a deterministic suffix order for modifiers."""
    suffixes = sorted({str(item).upper() for item in modifiers})
    suffix_pattern = '|'.join(re.escape(item) for item in suffixes)
    name = re.sub(r'(?:-(?:' + suffix_pattern + r'))+$', '', str(source_tag), flags=re.I)
    result = name + ''.join(f'-{suffix}' for suffix in suffixes)
    if len(result) > 64:
        raise ValueError('Тег inbound слишком длинный для суффикса GEMINI')
    return result


def find_inbound(config, tag):
    for inbound in config.get('inbounds') or []:
        if inbound.get('tag') == tag:
            return inbound
    raise ValueError('Исходный inbound не найден в конфигурации профиля')



def gemini_rules(tag):
    return [
        {'type': 'field', 'inboundTag': [tag], 'network': 'udp',
         'domain': GOOGLE_DOMAINS, 'outboundTag': BLOCK_TAG},
        {'type': 'field', 'inboundTag': [tag], 'network': 'tcp',
         'domain': GOOGLE_DOMAINS, 'outboundTag': SOCKS_TAG}]


def prepare_gemini_config(source_config, inbound_tag):
    """Append one same-port sibling; retain every original inbound and UUID mapping."""
    config = parse_config(source_config)
    inbound = find_inbound(config, inbound_tag)
    new_tag = modified_inbound_tag(inbound_tag)
    clone = copy.deepcopy(inbound)
    clone['tag'] = new_tag
    existing_inbound = next((item for item in config.get('inbounds') or [] if item.get('tag') == new_tag), None)
    if existing_inbound:
        if existing_inbound != clone or not has_gemini_routes(config, new_tag):
            raise ValueError('Существующий Gemini inbound отличается от исходного inbound или его маршруты не совпадают')
        rules = (config.get('routing') or {}).get('rules') or []
        expected_inherited = []
        actual_inherited = []
        for rule in rules:
            tags = rule.get('inboundTag')
            tags = [tags] if isinstance(tags, str) else tags
            if tags and inbound_tag in tags:
                expected_rule = copy.deepcopy(rule)
                expected_rule['inboundTag'] = [new_tag]
                expected_inherited.append(expected_rule)
            if tags == [new_tag] and rule not in gemini_rules(new_tag):
                actual_inherited.append(rule)
        if expected_inherited != actual_inherited:
            raise ValueError('У Gemini inbound не совпадают унаследованные маршруты; проверьте профиль')
        return config, new_tag
    config['inbounds'].append(clone)
    outbounds = config.setdefault('outbounds', [])
    expected = {'tag': SOCKS_TAG, 'protocol': 'socks', 'settings': {
        'address': SOCKS_ADDRESS, 'port': SOCKS_PORT}}
    existing = next((item for item in outbounds if item.get('tag') == SOCKS_TAG), None)
    if existing and existing != expected:
        raise ValueError('В профиле уже есть несовместимый outbound psiphon-out')
    if not existing:
        outbounds.append(expected)
    block = next((item for item in outbounds if item.get('tag') == BLOCK_TAG), None)
    if block and block.get('protocol') != 'blackhole':
        raise ValueError('В профиле outbound BLOCK не является blackhole')
    if not block:
        outbounds.append({'tag': BLOCK_TAG, 'protocol': 'blackhole', 'settings': {}})
    rules = config.setdefault('routing', {}).setdefault('rules', [])
    # Extend inherited rules to the sibling without removing the original tag.
    routes = gemini_rules(new_tag)
    # Place each inherited sibling rule next to its original, preserving priority.
    extended = []
    for rule in rules:
        extended.append(rule)
        tags = rule.get('inboundTag')
        tags = [tags] if isinstance(tags, str) else tags
        if tags and inbound_tag in tags:
            copied = copy.deepcopy(rule)
            copied['inboundTag'] = [new_tag]
            extended.append(copied)
    config['routing']['rules'] = routes + extended
    return config, new_tag


def has_gemini_routes(config, inbound_tag):
    try:
        config = parse_config(config)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    outbounds = {item.get('tag'): item for item in config.get('outbounds') or []}
    if outbounds.get(SOCKS_TAG) != {'tag': SOCKS_TAG, 'protocol': 'socks', 'settings': {
            'address': SOCKS_ADDRESS, 'port': SOCKS_PORT}}:
        return False
    found = {'tcp': False, 'udp': False}
    for rule in (config.get('routing') or {}).get('rules') or []:
        tags = rule.get('inboundTag') or []
        if isinstance(tags, str):
            tags = [tags]
        domains = set(rule.get('domain') or [])
        if inbound_tag not in tags or not set(GOOGLE_DOMAINS).issubset(domains):
            continue
        if rule.get('network') == 'tcp' and rule.get('outboundTag') == SOCKS_TAG:
            found['tcp'] = True
        if rule.get('network') == 'udp' and rule.get('outboundTag', '').upper() in {'BLOCK', 'BLACKHOLE'}:
            found['udp'] = True
    if not all(found.values()):
        return False
    # A name and two rules alone do not prove routing: a preceding catch-all can shadow them.
    pending = {'tcp', 'udp'}
    for rule in (config.get('routing') or {}).get('rules') or []:
        tags = rule.get('inboundTag') or []
        tags = [tags] if isinstance(tags, str) else tags
        if tags and inbound_tag not in tags:
            continue
        network = rule.get('network')
        covered = set(str(network).split(',')) if network else {'tcp', 'udp'}
        if not pending.intersection(covered):
            continue
        valid_tcp = network == 'tcp' and rule.get('outboundTag') == SOCKS_TAG
        target = outbounds.get(rule.get('outboundTag'), {})
        valid_udp = network == 'udp' and target.get('protocol') == 'blackhole'
        if not (valid_tcp or valid_udp) or not set(GOOGLE_DOMAINS).issubset(set(rule.get('domain') or [])):
            return False
        pending.discard(network)
        if not pending:
            return True
    return False


def inbound_uuid_by_tag(profile, tag):
    inbounds = profile.get('inbounds') or []
    match = next((item for item in inbounds if item.get('tag') == tag), None)
    if not match or not match.get('uuid'):
        raise ValueError('Панель не вернула UUID созданного inbound')
    return match['uuid']


def squad_inbound_list(squad):
    return [item.get('uuid') if isinstance(item, dict) else item
            for item in squad.get('inbounds') or []]


async def read_profile(api, profile_uuid):
    profile = await api.call('GET', f'/config-profiles/{profile_uuid}')
    if isinstance(profile, dict) and isinstance(profile.get('response'), dict):
        profile = profile['response']
    return profile


async def list_squads(api):
    result = await api.call('GET', '/internal-squads') or {}
    return result.get('internalSquads', []) if isinstance(result, dict) else result


async def add_inbound_to_squads(api, original_uuid, new_uuid):
    changed = []
    try:
        for squad in await list_squads(api):
            ids = squad_inbound_list(squad)
            if original_uuid in ids and new_uuid not in ids:
                changed.append({'uuid': squad['uuid'], 'original': ids})
                await api.call('PATCH', '/internal-squads', {
                    'uuid': squad['uuid'], 'inbounds': list(dict.fromkeys(ids + [new_uuid]))})
    except BaseException as original_error:
        try:
            await restore_squads(api, changed, original_uuid, new_uuid)
        except Exception as rollback_error:
            raise RuntimeError('Не удалось полностью откатить изменение squads') from rollback_error
        raise original_error
    return changed


async def restore_squads(api, changed, original_uuid, new_uuid):
    for item in changed:
        ids = item['original']
        # Preserve panel edits made after the operation, and remove only our inbound.
        current = next((s for s in await list_squads(api) if s['uuid'] == item['uuid']), None)
        if current:
            live = squad_inbound_list(current)
            restored = [value for value in live if value != new_uuid]
            for value in ids:
                if value not in restored:
                    restored.append(value)
            await api.call('PATCH', '/internal-squads', {'uuid': item['uuid'], 'inbounds': restored})


async def create_or_reuse_gemini_inbound(api, source_profile_uuid, source_inbound_uuid):
    source = await read_profile(api, source_profile_uuid)
    original_config = parse_config(source.get('config'))
    original = next((item for item in source.get('inbounds') or [] if item.get('uuid') == source_inbound_uuid), None)
    if not original:
        raise ValueError('Не удалось определить inbound исходного профиля')
    original_tag = original['tag']
    config, tag = prepare_gemini_config(original_config, original_tag)
    if config != original_config:
        latest = await read_profile(api, source_profile_uuid)
        if parse_config(latest.get('config')) != original_config:
            raise ValueError('Конфигурация профиля изменилась в панели; повторите операцию')
        await api.call('PATCH', '/config-profiles', {'uuid': source_profile_uuid, 'config': config})
    profile = await read_profile(api, source_profile_uuid)
    if parse_config(profile.get('config')) != config or not has_gemini_routes(profile.get('config'), tag):
        raise RuntimeError('Панель не подтвердила Gemini inbound в исходном профиле')
    # Existing hosts depend on these UUIDs: a config update must not recreate them.
    for inbound in source.get('inbounds') or []:
        if inbound_uuid_by_tag(profile, inbound['tag']) != inbound['uuid']:
            raise RuntimeError('Панель изменила UUID существующего inbound; проверьте привязки hosts')
    return profile, original_tag, tag, inbound_uuid_by_tag(profile, tag)


def active_inbound_ids(node):
    config_profile = node.get('configProfile') or {}
    return [item.get('uuid') if isinstance(item, dict) else item
            for item in config_profile.get('activeInbounds') or []]


async def switch_profile(api, node_uuid, profile_uuid, inbound_uuid):
    inbounds = [inbound_uuid] if isinstance(inbound_uuid, str) else list(inbound_uuid)
    await api.call('POST', '/nodes/bulk-actions/profile-modification', {
        'uuids': [node_uuid], 'configProfile': {
            'activeConfigProfileUuid': profile_uuid, 'activeInbounds': inbounds}})


def linked_hosts(hosts, node_uuid):
    return [host for host in hosts if node_uuid in [
        item.get('uuid') if isinstance(item, dict) else item for item in host.get('nodes') or []]]


def _host_for_node(hosts, node):
    matches = linked_hosts(hosts, node['uuid'])
    direct = next((host for host in matches if host.get('uuid') == node.get('hostUuid')), None)
    return direct or (matches[0] if len(matches) == 1 else None)


async def _panel_node_host(api, node_uuid, host_uuid=None):
    node = next((item for item in await api.nodes() if item.get('uuid') == node_uuid), None)
    if not node:
        raise ValueError('Нода не найдена в панели')
    hosts = await api.call('GET', '/hosts') or []
    host = next((h for h in linked_hosts(hosts, node_uuid) if h['uuid'] == host_uuid), None) if host_uuid else _host_for_node(hosts, node)
    if not host:
        raise ValueError('Выберите host ноды для настройки Gemini')
    if [item.get('uuid') if isinstance(item, dict) else item for item in host.get('nodes') or []] != [node_uuid]:
        raise ValueError('Host связан с несколькими нодами; настройте отдельный host для этой ноды')
    return node, host


async def job_host_uuid(service, session, node, job_id):
    from db.dal import app_settings_dal
    row = await app_settings_dal.get_setting(session, f'infra.gemini.job.{service.scope}.{job_id}')
    return row.value if row and row.value else getattr(node, 'host_uuid', None)


async def read_gemini_state(service, session, node, host_uuid):
    from db.dal import app_settings_dal
    key = f'infra.gemini.{service.scope}.{node.id}.{host_uuid}'
    row = await app_settings_dal.get_setting(session, key)
    if row and row.value:
        return key, json.loads(row.value)
    return key, {}


async def check_other_hosts(api, node_uuid, host_uuid, profile_uuid, inbound_uuid):
    hosts = linked_hosts(await api.call('GET', '/hosts') or [], node_uuid)
    if any(h['uuid'] != host_uuid and (h.get('inbound') or {}) == {
            'configProfileUuid': profile_uuid, 'configProfileInboundUuid': inbound_uuid} for h in hosts):
        raise ValueError('Другой host этой ноды использует тот же inbound; создайте для него отдельный inbound перед переключением')


async def assert_selection_unchanged(api, expected_node, host):
    current, selected = await _panel_node_host(api, expected_node['uuid'], host['uuid'])
    if (current.get('address') != expected_node.get('address')
            or (current.get('configProfile') or {}).get('activeConfigProfileUuid') !=
                (expected_node.get('configProfile') or {}).get('activeConfigProfileUuid')
            or set(active_inbound_ids(current)) != set(active_inbound_ids(expected_node))
            or selected.get('inbound') != host.get('inbound')):
        raise ValueError('Нода или host изменены в панели во время настройки Gemini; повторите операцию')


async def verify_connected(api, node_uuid):
    """Wait for three stable panel connection observations after restarting the core."""
    import asyncio
    streak = 0
    for _attempt in range(30):
        current = next((n for n in await api.nodes() if n['uuid'] == node_uuid), None)
        if current and current.get('isConnected') and not current.get('isConnecting'):
            streak += 1
            if streak >= 3:
                return
        else:
            streak = 0
        await asyncio.sleep(2)
    raise RuntimeError('Ядро ноды не подключилось после переключения Gemini')


async def apply_gemini_route(service, session, node, deployment, config, job_id, admin_id):
    """Install the local proxy, fork the current inbound, and atomically rebind the same host."""
    from db.dal import app_settings_dal
    from .provisioning import ansible, stage

    api = service.api
    host_uuid = await job_host_uuid(service, session, node, job_id)
    panel_node, host = await _panel_node_host(api, node.panel_uuid, host_uuid)
    current_profile = panel_node.get('configProfile') or {}
    original_profile = current_profile.get('activeConfigProfileUuid')
    original_inbounds = active_inbound_ids(panel_node)
    original_host_inbound = copy.deepcopy(host.get('inbound'))
    source_inbound = original_host_inbound or {}
    selected_id = source_inbound.get('configProfileInboundUuid')
    if (not original_profile or source_inbound.get('configProfileUuid') != original_profile
            or selected_id not in original_inbounds):
        raise ValueError('Host использует другой inbound, чем выбран у ноды; обновите карточку и повторите')

    source = await read_profile(api, original_profile)
    active_inbound = next((entry for entry in source.get('inbounds') or []
                           if entry.get('uuid') == selected_id), None)
    active_tag = active_inbound.get('tag') if active_inbound else None
    if not active_tag:
        raise ValueError('Не удалось определить tag активного inbound в профиле')
    if 'gemini' in active_tag.casefold():
        source_config = parse_config(source.get('config'))
        actual_tag = active_tag
        if not actual_tag or not has_gemini_routes(source_config, actual_tag):
            raise ValueError('У выбранного в панели Gemini inbound не найден подтверждённый TCP-маршрут Google через Psiphon')
        await stage(service, job_id, 'psiphon')
        await ansible(service, deployment, config, kind='gemini')
        await assert_selection_unchanged(api, panel_node, host)
        await verify_connected(api, node.panel_uuid)
        key, saved = await read_gemini_state(service, session, node, host['uuid'])
        state = dict(saved, host_uuid=host['uuid'], enabled=True, gemini_profile_uuid=original_profile,
            gemini_inbound_uuid=selected_id, gemini_inbound_uuids=original_inbounds)
        base_tag = re.sub(r'-GEMINI$', '', actual_tag, flags=re.I)
        base = next((entry for entry in source.get('inbounds') or [] if entry.get('tag') == base_tag), None)
        if base and base_tag != actual_tag:
            base_config, sibling_tag = prepare_gemini_config(source_config, base_tag)
            if sibling_tag == actual_tag and base_config == source_config:
                state.update(layout='inbound', base_profile_uuid=original_profile,
                    base_inbound_uuid=base['uuid'], source_tag=base_tag, gemini_tag=actual_tag,
                    base_host_inbound={'configProfileUuid': original_profile, 'configProfileInboundUuid': base['uuid']},
                    base_inbound_uuids=[base['uuid'] if identifier == selected_id else identifier for identifier in original_inbounds])
        state['panel_selected'] = not bool(state.get('base_inbound_uuid'))
        await app_settings_dal.upsert_setting(session, key, json.dumps(state), updated_by=admin_id)
        await session.commit()
        await stage(service, job_id, 'gemini_done')
        return

    await check_other_hosts(api, node.panel_uuid, host['uuid'], original_profile, selected_id)
    await stage(service, job_id, 'psiphon')
    await ansible(service, deployment, config, kind='gemini')

    await assert_selection_unchanged(api, panel_node, host)
    await stage(service, job_id, 'gemini_profile')
    profile, source_tag, modified_tag, target_inbound = await create_or_reuse_gemini_inbound(
        api, original_profile, selected_id)
    target_inbounds = [target_inbound if identifier == selected_id else identifier for identifier in original_inbounds]
    target_inbounds = list(dict.fromkeys(target_inbounds))
    expected_tag = find_inbound(parse_config(profile.get('config')), modified_tag)
    for other_id in target_inbounds:
        if other_id != target_inbound:
            tag = next((i['tag'] for i in profile.get('inbounds') or [] if i['uuid'] == other_id), None)
            other = find_inbound(parse_config(profile.get('config')), tag)
            if other.get('port') == expected_tag.get('port') and other.get('listen', '0.0.0.0') == expected_tag.get('listen', '0.0.0.0'):
                raise ValueError('Другой активный inbound слушает тот же адрес и порт; переключение отменено')
    changed_squads = []
    switched_node = switched_host = False
    try:
        for old_id, new_id in zip(original_inbounds, target_inbounds):
            changes = await add_inbound_to_squads(api, old_id, new_id)
            changed_squads.append({'original_uuid': old_id, 'new_uuid': new_id, 'changes': changes})
        await assert_selection_unchanged(api, panel_node, host)
        await stage(service, job_id, 'gemini_switch')
        switched_node = True
        await switch_profile(api, node.panel_uuid, profile['uuid'], target_inbounds)
        switched_host = True
        await api.call('PATCH', '/hosts', {'uuid': host['uuid'], 'inbound': {
            'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': target_inbound}})
        current_node, current_host = await _panel_node_host(api, node.panel_uuid, host['uuid'])
        if ((current_node.get('configProfile') or {}).get('activeConfigProfileUuid') != profile['uuid']
                or set(active_inbound_ids(current_node)) != set(target_inbounds)
                or current_host.get('inbound') != {'configProfileUuid': profile['uuid'],
                    'configProfileInboundUuid': target_inbound}):
            raise RuntimeError('Панель не подтвердила переключение host на Gemini inbound')
        await api.restart_node(node.panel_uuid, force=True)
        await verify_connected(api, node.panel_uuid)
        state = {'layout': 'inbound', 'host_uuid': host['uuid'], 'base_profile_uuid': original_profile, 'base_inbound_uuid': selected_id,
            'base_inbound_uuids': original_inbounds, 'gemini_inbound_uuids': target_inbounds,
            'base_host_inbound': original_host_inbound, 'gemini_profile_uuid': profile['uuid'],
            'gemini_inbound_uuid': target_inbound, 'source_tag': source_tag,
            'gemini_tag': modified_tag, 'squads': changed_squads, 'enabled': True}
        await app_settings_dal.upsert_setting(session, f'infra.gemini.{service.scope}.{node.id}.{host['uuid']}',
            json.dumps(state), updated_by=admin_id)
        await session.commit()
        await stage(service, job_id, 'gemini_done')
    except BaseException as original_error:
        # Compensate in reverse order. Keep the inactive sibling for a safe retry.
        rollback_errors = []
        try:
            if switched_host and original_host_inbound:
                await api.call('PATCH', '/hosts', {'uuid': host['uuid'], 'inbound': original_host_inbound})
        except Exception as exc:
            rollback_errors.append(exc)
        try:
            if switched_node:
                await switch_profile(api, node.panel_uuid, original_profile, original_inbounds)
                await api.restart_node(node.panel_uuid, force=True)
                await verify_connected(api, node.panel_uuid)
        except Exception as exc:
            rollback_errors.append(exc)
        try:
            for change in reversed(changed_squads):
                await restore_squads(api, change['changes'], change['original_uuid'], change['new_uuid'])
        except Exception as exc:
            rollback_errors.append(exc)
        if rollback_errors:
            raise RuntimeError('Переключение Gemini не завершилось, автоматический откат требует проверки панели') from original_error
        raise


async def remove_gemini_route(service, session, node, job_id, admin_id):
    """Restore only this host's source inbound, preserving other active inbounds."""
    from db.dal import app_settings_dal
    from .provisioning import stage

    api = service.api
    host_uuid = await job_host_uuid(service, session, node, job_id)
    panel_node, host = await _panel_node_host(api, node.panel_uuid, host_uuid)
    key, state = await read_gemini_state(service, session, node, host['uuid'])
    if not state.get('base_profile_uuid') or not state.get('base_inbound_uuid'):
        raise ValueError('Исходный inbound Gemini-маршрута не сохранён; выберите его вручную в панели')
    live_profile = (panel_node.get('configProfile') or {}).get('activeConfigProfileUuid')
    live_inbounds = active_inbound_ids(panel_node)
    old_host_inbound = copy.deepcopy(host.get('inbound'))
    if (state.get('layout') != 'inbound' or state['gemini_inbound_uuid'] not in live_inbounds
            or live_profile != state['gemini_profile_uuid']
            or old_host_inbound != {'configProfileUuid': state['gemini_profile_uuid'],
                                    'configProfileInboundUuid': state['gemini_inbound_uuid']}):
        raise ValueError('Нода или host вручную изменены в панели после включения Gemini; сначала синхронизируйте панель')
    await check_other_hosts(api, node.panel_uuid, host['uuid'], live_profile, state['gemini_inbound_uuid'])
    original_inbounds = list(dict.fromkeys(state['base_inbound_uuid'] if identifier == state['gemini_inbound_uuid']
        else identifier for identifier in live_inbounds))
    await assert_selection_unchanged(api, panel_node, host)
    await stage(service, job_id, 'gemini_switch')
    switched_node = switched_host = False
    try:
        switched_node = True
        await switch_profile(api, node.panel_uuid, state['base_profile_uuid'], original_inbounds)
        switched_host = True
        await api.call('PATCH', '/hosts', {'uuid': host['uuid'], 'inbound': state['base_host_inbound']})
        checked_node, checked_host = await _panel_node_host(api, node.panel_uuid, host['uuid'])
        if ((checked_node.get('configProfile') or {}).get('activeConfigProfileUuid') != state['base_profile_uuid']
                or set(active_inbound_ids(checked_node)) != set(original_inbounds)
                or checked_host.get('inbound') != state['base_host_inbound']):
            raise RuntimeError('Панель не подтвердила возврат исходного host')
        await api.restart_node(node.panel_uuid, force=True)
        await verify_connected(api, node.panel_uuid)
        state['enabled'] = False
        await app_settings_dal.upsert_setting(session, key, json.dumps(state), updated_by=admin_id)
        await app_settings_dal.upsert_setting(session,
            f'infra.gemini.detected.{service.scope}.{node.id}.{host["uuid"]}', '', updated_by=admin_id)
        await session.commit()
        await stage(service, job_id, 'gemini_done')
    except BaseException as original_error:
        rollback_errors = []
        try:
            if switched_host and old_host_inbound:
                await api.call('PATCH', '/hosts', {'uuid': host['uuid'], 'inbound': old_host_inbound})
        except Exception as exc:
            rollback_errors.append(exc)
        try:
            if switched_node and live_profile and live_inbounds:
                await switch_profile(api, node.panel_uuid, live_profile, live_inbounds)
                await api.restart_node(node.panel_uuid, force=True)
                await verify_connected(api, node.panel_uuid)
        except Exception as exc:
            rollback_errors.append(exc)
        if rollback_errors:
            raise RuntimeError('Возврат Gemini-маршрута не завершился, автоматический откат требует проверки панели') from original_error
        raise


async def detect_panel_selected_gemini(service, session, nodes):
    """Install Psiphon for routed hosts selected in-panel, including multi-host nodes."""
    from sqlalchemy import select
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from db.dal import app_settings_dal
    from db.models import InfrastructureJob as Job

    queued = []
    if not nodes:
        return queued
    panel_nodes = {item['uuid']: item for item in await service.api.nodes()}
    hosts = await service.api.call('GET', '/hosts') or []
    profiles = {}
    for node in nodes:
        current = panel_nodes.get(node.panel_uuid)
        if not current:
            continue
        profile_uuid = (current.get('configProfile') or {}).get('activeConfigProfileUuid')
        ids = active_inbound_ids(current)
        if not profile_uuid or not ids:
            continue
        if profile_uuid not in profiles:
            profiles[profile_uuid] = await read_profile(service.api, profile_uuid)
        profile = profiles[profile_uuid]
        for host in linked_hosts(hosts, node.panel_uuid):
            binding = host.get('inbound') or {}
            selected_id = binding.get('configProfileInboundUuid')
            if host.get('isDisabled') or selected_id not in ids or binding.get('configProfileUuid') != profile_uuid:
                continue
            marker_key = f'infra.gemini.detected.{service.scope}.{node.id}.{host["uuid"]}'
            selection = f'{profile_uuid}:{selected_id}'
            marker = await app_settings_dal.get_setting(session, marker_key)
            if marker and marker.value and marker.value.split(':queued')[0] != selection:
                await app_settings_dal.upsert_setting(session, marker_key, '')
                marker = None
            inbound = next((i for i in profile.get('inbounds') or [] if i['uuid'] == selected_id), None)
            if not inbound or 'gemini' not in str(inbound.get('tag') or '').casefold():
                continue
            if not has_gemini_routes(profile.get('config'), inbound['tag']):
                continue
            _key, state = await read_gemini_state(service, session, node, host['uuid'])
            if state.get('enabled') and state.get('gemini_profile_uuid') == profile_uuid and state.get('gemini_inbound_uuid') == selected_id:
                continue
            deployment = await service.deployment(session, node.id)
            if marker and (marker.value == selection + ':queued' or (marker.value == selection and not deployment)):
                continue
            active = (await session.execute(select(Job.id).where(Job.node_id == node.id,
                Job.kind.in_(['gemini_enable', 'gemini_disable']), Job.status.in_(['pending', 'running'])))).first()
            if active:
                # Do not mark an unrelated host handled: retry it after the active operation.
                continue
            if not deployment:
                for admin in service.settings.ADMIN_IDS:
                    try:
                        from .localization import admin_translator
                        translate = await admin_translator(service, session, admin)
                        await service.bot.send_message(admin, translate('infra_gemini_ssh_required', p0=node.name),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                text=translate('infra_save_access'), callback_data=f'infra:access:{node.id}')]]))
                    except Exception:
                        pass
                await app_settings_dal.upsert_setting(session, marker_key, selection)
                continue
            job = await service.enqueue(session, node, deployment, 'gemini_enable', service.settings.ADMIN_IDS[0], host_uuid=host['uuid'])
            await app_settings_dal.upsert_setting(session, marker_key, selection + ':queued')
            queued.append(job.id)
    return queued
