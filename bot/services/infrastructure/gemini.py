"""Gemini-only Psiphon profile modifiers for Remnawave panels."""
import copy
import hashlib
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


def modified_profile_name(source_name, modifiers=(MODIFIER,)):
    """Return a deterministic, alphabetically ordered suffix chain."""
    name = re.sub(r'(?:-[A-Z0-9]+)+$', '', str(source_name)).rstrip('-')
    suffixes = sorted({str(item).upper() for item in modifiers})
    result = name + ''.join(f'-{suffix}' for suffix in suffixes)
    if len(result) > 30:
        raise ValueError('Название профиля слишком длинное для суффикса GEMINI')
    return result


def find_inbound(config, tag):
    for inbound in config.get('inbounds') or []:
        if inbound.get('tag') == tag:
            return inbound
    raise ValueError('Исходный inbound не найден в конфигурации профиля')


def fork_inbound_tag(tag, selected_tag):
    suffix = '-GEMINI' if tag == selected_tag else '-FORK-' + hashlib.sha256(selected_tag.encode()).hexdigest()[:8]
    result = tag + suffix
    if len(result) > 64:
        raise ValueError('Тег inbound слишком длинный для суффикса GEMINI')
    return result


def prepare_gemini_config(source_config, inbound_tag):
    """Copy a source profile and route Google TCP for one inbound via Psiphon."""
    config = parse_config(source_config)
    inbound = find_inbound(config, inbound_tag)
    new_tag = fork_inbound_tag(inbound_tag, inbound_tag)
    renamed = {entry['tag']: fork_inbound_tag(entry['tag'], inbound_tag) for entry in config.get('inbounds') or []}
    for entry in config.get('inbounds') or []:
        entry['tag'] = renamed[entry['tag']]

    outbounds = config.setdefault('outbounds', [])
    existing = next((item for item in outbounds if item.get('tag') == SOCKS_TAG), None)
    expected = {'tag': SOCKS_TAG, 'protocol': 'socks', 'settings': {
        'address': SOCKS_ADDRESS, 'port': SOCKS_PORT}}
    if existing and existing != expected:
        raise ValueError('В профиле уже есть несовместимый outbound psiphon-out')
    if not existing:
        outbounds.append(expected)
    block = next((item for item in outbounds if item.get('tag') == BLOCK_TAG), None)
    if block and block.get('protocol') != 'blackhole':
        raise ValueError('В профиле outbound BLOCK не является blackhole')
    if not block:
        outbounds.append({'tag': BLOCK_TAG, 'protocol': 'blackhole', 'settings': {}})

    routing = config.setdefault('routing', {})
    rules = routing.setdefault('rules', [])
    for rule in rules:
        tags = rule.get('inboundTag')
        if isinstance(tags, list):
            rule['inboundTag'] = [renamed.get(tag, tag) for tag in tags]
        elif isinstance(tags, str):
            rule['inboundTag'] = [renamed.get(tags, tags)]
    route_rules = [
        {'type': 'field', 'inboundTag': [new_tag], 'network': 'udp',
         'domain': GOOGLE_DOMAINS, 'outboundTag': BLOCK_TAG},
        {'type': 'field', 'inboundTag': [new_tag], 'network': 'tcp',
         'domain': GOOGLE_DOMAINS, 'outboundTag': SOCKS_TAG},
    ]
    # Prevent an inherited catch-all inbound rule from shadowing the new routes.
    rules[0:0] = route_rules
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
    return all(found.values())


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


async def create_or_reuse_gemini_profile(api, source_profile_uuid, source_inbound_uuid):
    source = await read_profile(api, source_profile_uuid)
    source_config = parse_config(source.get('config'))
    inbounds = source.get('inbounds') or []
    source_inbound = next((item for item in inbounds if item.get('uuid') == source_inbound_uuid), None)
    if not source_inbound:
        raise ValueError('Не удалось определить inbound исходного профиля')
    original_tag = source_inbound.get('tag')
    config, modified_tag = prepare_gemini_config(source_config, original_tag)
    name = modified_profile_name(source.get('name'))
    profiles = await api.profiles()
    existing = next((item for item in profiles if item.get('name', '').casefold() == name.casefold()), None)
    if existing:
        profile = await read_profile(api, existing['uuid'])
        if not has_gemini_routes(profile.get('config'), modified_tag):
            raise ValueError(f'Профиль {name} уже существует, но его маршруты Gemini не совпадают; измените его вручную или переименуйте')
        if parse_config(profile.get('config')) != config:
            raise ValueError(f'Профиль {name} отличается от текущего исходного конфига; измените его вручную или переименуйте')
    else:
        profile = await api.call('POST', '/config-profiles', {'name': name, 'config': config})
        if isinstance(profile, dict) and isinstance(profile.get('response'), dict):
            profile = profile['response']
        if not profile.get('uuid'):
            raise RuntimeError('Панель не вернула созданный профиль Gemini')
        profile = await read_profile(api, profile['uuid'])
    if not has_gemini_routes(profile.get('config'), modified_tag):
        raise ValueError('Не удалось подтвердить маршруты Google в профиле Gemini')
    return profile, original_tag, modified_tag, inbound_uuid_by_tag(profile, modified_tag)


def active_inbound_ids(node):
    config_profile = node.get('configProfile') or {}
    return [item.get('uuid') if isinstance(item, dict) else item
            for item in config_profile.get('activeInbounds') or []]


async def switch_profile(api, node_uuid, profile_uuid, inbound_uuid):
    inbounds = [inbound_uuid] if isinstance(inbound_uuid, str) else list(inbound_uuid)
    await api.call('POST', '/nodes/bulk-actions/profile-modification', {
        'uuids': [node_uuid], 'configProfile': {
            'activeConfigProfileUuid': profile_uuid, 'activeInbounds': inbounds}})


def mapped_active_inbounds(source, target, active_ids, selected_id, modified_tag):
    tags = {entry['uuid']: entry['tag'] for entry in source.get('inbounds') or []}
    result = []
    for identifier in active_ids:
        if identifier not in tags:
            raise ValueError('Не удалось определить все активные inbound исходного профиля')
        tag = modified_tag if identifier == selected_id else fork_inbound_tag(tags[identifier], tags[selected_id])
        result.append(inbound_uuid_by_tag(target, tag))
    return result


def _host_for_node(hosts, node):
    direct = next((host for host in hosts if host.get('uuid') == node.get('hostUuid')), None)
    if direct:
        return direct
    matches = [host for host in hosts if node.get('uuid') in [
        item.get('uuid') if isinstance(item, dict) else item for item in host.get('nodes', [])]]
    return matches[0] if len(matches) == 1 else None


async def _panel_node_host(api, node_uuid):
    node = next((item for item in await api.nodes() if item.get('uuid') == node_uuid), None)
    if not node:
        raise ValueError('Нода не найдена в панели')
    hosts = await api.call('GET', '/hosts') or []
    host = _host_for_node(hosts, node)
    if not host:
        raise ValueError('Для ноды не найден связанный host')
    return node, host


async def apply_gemini_route(service, session, node, deployment, config, job_id, admin_id):
    """Install the local proxy, fork the current inbound, and atomically rebind the same host."""
    from db.dal import app_settings_dal
    from .provisioning import ansible, stage

    api = service.api
    panel_node, host = await _panel_node_host(api, node.panel_uuid)
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
        key = f'infra.gemini.{service.scope}.{node.id}'
        previous = await app_settings_dal.get_setting(session, key)
        saved = json.loads(previous.value) if previous and previous.value else {}
        state = {'base_profile_uuid': saved.get('base_profile_uuid'),
            'base_inbound_uuid': saved.get('base_inbound_uuid'),
            'base_host_inbound': saved.get('base_host_inbound'),
            'base_inbound_uuids': saved.get('base_inbound_uuids'),
            'gemini_profile_uuid': original_profile, 'gemini_inbound_uuid': selected_id,
            'gemini_inbound_uuids': original_inbounds,
            'source_tag': actual_tag, 'gemini_tag': actual_tag, 'squads': [], 'enabled': True,
            'panel_selected': not bool(saved.get('base_profile_uuid') and saved.get('base_inbound_uuid'))}
        await app_settings_dal.upsert_setting(session, key, json.dumps(state), updated_by=admin_id)
        await session.commit()
        await stage(service, job_id, 'gemini_done')
        return

    await stage(service, job_id, 'psiphon')
    await ansible(service, deployment, config, kind='gemini')

    await stage(service, job_id, 'gemini_profile')
    profile, source_tag, modified_tag, target_inbound = await create_or_reuse_gemini_profile(
        api, original_profile, selected_id)
    target_inbounds = mapped_active_inbounds(source, profile, original_inbounds, selected_id, modified_tag)
    changed_squads = []
    switched_node = switched_host = False
    try:
        for old_id, new_id in zip(original_inbounds, target_inbounds):
            changes = await add_inbound_to_squads(api, old_id, new_id)
            changed_squads.append({'original_uuid': old_id, 'new_uuid': new_id, 'changes': changes})
        await stage(service, job_id, 'gemini_switch')
        switched_node = True
        await switch_profile(api, node.panel_uuid, profile['uuid'], target_inbounds)
        switched_host = True
        await api.call('PATCH', '/hosts', {'uuid': host['uuid'], 'inbound': {
            'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': target_inbound}})
        current_node, current_host = await _panel_node_host(api, node.panel_uuid)
        if ((current_node.get('configProfile') or {}).get('activeConfigProfileUuid') != profile['uuid']
                or set(active_inbound_ids(current_node)) != set(target_inbounds)
                or current_host.get('inbound') != {'configProfileUuid': profile['uuid'],
                    'configProfileInboundUuid': target_inbound}):
            raise RuntimeError('Панель не подтвердила переключение host на Gemini inbound')
        state = {'base_profile_uuid': original_profile, 'base_inbound_uuid': selected_id,
            'base_inbound_uuids': original_inbounds, 'gemini_inbound_uuids': target_inbounds,
            'base_host_inbound': original_host_inbound, 'gemini_profile_uuid': profile['uuid'],
            'gemini_inbound_uuid': target_inbound, 'source_tag': source_tag,
            'gemini_tag': modified_tag, 'squads': changed_squads, 'enabled': True}
        await app_settings_dal.upsert_setting(session, f'infra.gemini.{service.scope}.{node.id}',
            json.dumps(state), updated_by=admin_id)
        await session.commit()
        await stage(service, job_id, 'gemini_done')
    except BaseException as original_error:
        # Compensate in reverse order. Preserve the modifier profile for diagnosis/retry.
        rollback_errors = []
        try:
            if switched_host and original_host_inbound:
                await api.call('PATCH', '/hosts', {'uuid': host['uuid'], 'inbound': original_host_inbound})
        except Exception as exc:
            rollback_errors.append(exc)
        try:
            if switched_node:
                await switch_profile(api, node.panel_uuid, original_profile, original_inbounds)
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
    """Restore the exact pre-modifier profile and inbound on the same host."""
    from db.dal import app_settings_dal
    from .provisioning import stage

    key = f'infra.gemini.{service.scope}.{node.id}'
    row = await app_settings_dal.get_setting(session, key)
    if not row or not row.value:
        raise ValueError('Исходный профиль Gemini-маршрута не сохранён; отключите inbound вручную в панели')
    state = json.loads(row.value)
    if not state.get('base_profile_uuid') or not state.get('base_inbound_uuid'):
        raise ValueError('Gemini inbound был выбран вручную в панели. Верните исходный профиль через панель, затем обновите карточку')
    api = service.api
    panel_node, host = await _panel_node_host(api, node.panel_uuid)
    live_profile = (panel_node.get('configProfile') or {}).get('activeConfigProfileUuid')
    live_inbounds = active_inbound_ids(panel_node)
    original_inbounds = state.get('base_inbound_uuids') or [state['base_inbound_uuid']]
    target_inbounds = state.get('gemini_inbound_uuids') or [state['gemini_inbound_uuid']]
    old_host_inbound = copy.deepcopy(host.get('inbound'))
    if (live_profile != state['gemini_profile_uuid'] or set(live_inbounds) != set(target_inbounds)
            or old_host_inbound != {'configProfileUuid': state['gemini_profile_uuid'],
                                    'configProfileInboundUuid': state['gemini_inbound_uuid']}):
        raise ValueError('Нода или host вручную изменены в панели после включения Gemini; сначала синхронизируйте панель')
    await stage(service, job_id, 'gemini_switch')
    switched_node = switched_host = False
    try:
        switched_node = True
        await switch_profile(api, node.panel_uuid, state['base_profile_uuid'], original_inbounds)
        switched_host = True
        await api.call('PATCH', '/hosts', {'uuid': host['uuid'], 'inbound': state['base_host_inbound']})
        checked_node, checked_host = await _panel_node_host(api, node.panel_uuid)
        if ((checked_node.get('configProfile') or {}).get('activeConfigProfileUuid') != state['base_profile_uuid']
                or set(active_inbound_ids(checked_node)) != set(original_inbounds)
                or checked_host.get('inbound') != state['base_host_inbound']):
            raise RuntimeError('Панель не подтвердила возврат исходного host')
        state['enabled'] = False
        await app_settings_dal.upsert_setting(session, key, json.dumps(state), updated_by=admin_id)
        await app_settings_dal.upsert_setting(session,
            f'infra.gemini.detected.{service.scope}.{node.id}', '', updated_by=admin_id)
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
        except Exception as exc:
            rollback_errors.append(exc)
        if rollback_errors:
            raise RuntimeError('Возврат Gemini-маршрута не завершился, автоматический откат требует проверки панели') from original_error
        raise


async def detect_panel_selected_gemini(service, session, nodes):
    """Queue Psiphon setup when a managed node is switched to a routed Gemini inbound in-panel."""
    from sqlalchemy import select
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from db.dal import app_settings_dal
    from db.models import InfrastructureJob as Job

    queued = []
    if not nodes:
        return queued
    panel_nodes = {item.get('uuid'): item for item in await service.api.nodes()}
    hosts = await service.api.call('GET', '/hosts') or []
    for node in nodes:
        current = panel_nodes.get(node.panel_uuid)
        if not current:
            continue
        profile_info = current.get('configProfile') or {}
        profile_uuid = profile_info.get('activeConfigProfileUuid')
        ids = active_inbound_ids(current)
        if not profile_uuid or not ids:
            continue
        host = _host_for_node(hosts, current)
        selected_id = (host.get('inbound') or {}).get('configProfileInboundUuid') if host else None
        if not host or host.get('isDisabled') or selected_id not in ids or (host.get('inbound') or {}).get('configProfileUuid') != profile_uuid:
            continue
        marker_key = f'infra.gemini.detected.{service.scope}.{node.id}'
        marker = await app_settings_dal.get_setting(session, marker_key)
        current_selection = f'{profile_uuid}:{selected_id}'
        if marker and marker.value and marker.value.split(':queued')[0] != current_selection:
            await app_settings_dal.upsert_setting(session, marker_key, '')
            marker = None
        profile = await read_profile(service.api, profile_uuid)
        inbound = next((item for item in profile.get('inbounds') or [] if item.get('uuid') == selected_id), None)
        if not inbound or 'gemini' not in str(inbound.get('tag') or '').casefold():
            continue
        config = parse_config(profile.get('config'))
        if not has_gemini_routes(config, inbound['tag']):
            continue

        state_key = f'infra.gemini.{service.scope}.{node.id}'
        state_row = await app_settings_dal.get_setting(session, state_key)
        state = json.loads(state_row.value) if state_row and state_row.value else {}
        if state.get('enabled') and state.get('gemini_profile_uuid') == profile_uuid \
                and state.get('gemini_inbound_uuid') == selected_id:
            continue
        marker = await app_settings_dal.get_setting(session, marker_key)
        marker_value = f'{profile_uuid}:{selected_id}'
        deployment = await service.deployment(session, node.id)
        if marker and marker.value == marker_value + ':queued':
            continue
        if marker and marker.value == marker_value and not deployment:
            continue

        active = (await session.execute(select(Job.id).where(Job.node_id == node.id,
            Job.kind == 'gemini_enable', Job.status.in_(['pending', 'running'])))).first()
        if active:
            await app_settings_dal.upsert_setting(session, marker_key, marker_value + ':queued')
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
            await app_settings_dal.upsert_setting(session, marker_key, marker_value)
            continue
        admin_id = service.settings.ADMIN_IDS[0]
        job = await service.enqueue(session, node, deployment, 'gemini_enable', admin_id)
        await app_settings_dal.upsert_setting(session, marker_key, marker_value + ':queued')
        queued.append(job.id)
    return queued
