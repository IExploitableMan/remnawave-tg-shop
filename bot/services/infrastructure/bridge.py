"""haproxy SNI fronting bridges on the shared bridge server.

haproxy owns port 443 on the bridge server and routes by SNI: every bridge
forwards its SNI to the bridged node's address, extra routes point at their
configured backends, the wildcard's mask domain is routed to the local
wildcard backend automatically, and unmatched SNIs fall through to the local
wildcard as well — REALITY masks them by proxying to the real site, which an
SNI-based reject would fingerprint — the server
holds no routing state of its own.

A bridge host points at the bridge address bound to the VLESS-WILDCARD
inbound with an SNI override, so subscribers ride the bridge on the
wildcard's REALITY keypair — which means the bridged node itself must
terminate the wildcard: the bot switches it (and its direct host, plus squad
memberships) onto the wildcard listener and reverses everything on removal.
The bridge node never runs the wildcard (haproxy owns its 443; at most a
local 8443 serves the mask route) and is never switched or restarted by the
bot. The bot keeps the wildcard's serverNames equal to the whole SNI routing
table (operator extras plus one entry per registered bridge).
"""
import asyncio
import copy
import json
import ssl

from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

from db.models import InfrastructureDeployment as Deployment

from .gemini import (read_profile, switch_profile, linked_hosts, active_inbound_ids,
    add_inbound_to_squads, restore_squads, check_other_hosts, assert_selection_unchanged,
    verify_connected, read_gemini_state)
from .settings import validate_hostname, validate_route, validate_backend

WILDCARD_TAG = 'VLESS-WILDCARD'


def bridge_host_remark(country_code):
    """Bridge hosts keep the node's host name with a link marker: 🇳🇱 Netherlands 🔗."""
    from .host_names import country_host_base
    return f'{country_host_base(country_code)} 🔗'


def _host_nodes(host):
    return [item.get('uuid') if isinstance(item, dict) else item for item in host.get('nodes', [])]


def wildcard_variants(profile_config):
    """The wildcard and its Gemini sibling share one serverNames list."""
    return [entry for entry in profile_config.get('inbounds') or []
        if entry.get('tag', '').casefold() in {WILDCARD_TAG.casefold(), (WILDCARD_TAG + '-GEMINI').casefold()}]


def _wildcard_uuid(profile):
    for item in profile.get('inbounds') or []:
        if item.get('tag') == WILDCARD_TAG:
            return item.get('uuid')
    return None


def wildcard_binding(profile_uuid, wildcard_uuid):
    return {'configProfileUuid': profile_uuid, 'configProfileInboundUuid': wildcard_uuid}


def _wildcard_names(profile_config):
    return {name for entry in wildcard_variants(profile_config)
        for name in (entry.get('streamSettings') or {}).get('realitySettings', {}).get('serverNames') or []}


async def add_profile_sni(api, profile_uuid, sni):
    """Append the SNI to every wildcard variant without touching other inbounds."""
    profile = await read_profile(api, profile_uuid)
    profile_config = profile.get('config')
    if isinstance(profile_config, str):
        profile_config = json.loads(profile_config)
    changed = False
    for entry in wildcard_variants(profile_config):
        reality = (entry.get('streamSettings') or {}).get('realitySettings') or {}
        names = list(reality.get('serverNames') or [])
        if sni not in names:
            reality['serverNames'] = names + [sni]
            entry['streamSettings']['realitySettings'] = reality
            changed = True
    if not changed:
        return False
    await api.call('PATCH', '/config-profiles', {'uuid': profile_uuid, 'config': profile_config})
    return True


async def remove_profile_sni(api, profile_uuid, sni):
    """Drop the SNI from every wildcard variant; other inbounds stay untouched."""
    profile = await read_profile(api, profile_uuid)
    profile_config = profile.get('config')
    if isinstance(profile_config, str):
        profile_config = json.loads(profile_config)
    changed = False
    for entry in wildcard_variants(profile_config):
        reality = (entry.get('streamSettings') or {}).get('realitySettings') or {}
        names = list(reality.get('serverNames') or [])
        if sni in names:
            reality['serverNames'] = [name for name in names if name != sni]
            entry['streamSettings']['realitySettings'] = reality
            changed = True
    if changed:
        await api.call('PATCH', '/config-profiles', {'uuid': profile_uuid, 'config': profile_config})
    return changed


async def sync_wildcard_snis(api, profile_uuid, snis):
    """Add every routing-table SNI missing from the wildcard serverNames."""
    profile = await read_profile(api, profile_uuid)
    profile_config = profile.get('config')
    if isinstance(profile_config, str):
        profile_config = json.loads(profile_config)
    if not wildcard_variants(profile_config):
        raise ValueError('В профиле нет inbound VLESS-WILDCARD')
    missing = [sni for sni in dict.fromkeys(snis) if sni not in _wildcard_names(profile_config)]
    if not missing:
        return False
    changed = False
    for sni in missing:
        changed = await add_profile_sni(api, profile_uuid, sni) or changed
    return changed


async def _bridge_infra_node(service, config):
    """The infra node deployed at the bridge address — the bridge node."""
    address = (config['bridge_address'] or '').casefold()
    async with service.factory() as session:
        for candidate in await service.nodes(session):
            deployment = await service.deployment(session, candidate.id)
            if deployment and (deployment.address or '').casefold() == address:
                return candidate
    return None


async def ensure_bridge_wildcard(service, config):
    """Locate the VLESS-WILDCARD inbound on the bridge node's active profile.

    haproxy owns port 443 and routes bridged SNIs to the wildcard backend; the
    bot never activates the wildcard on the bridge node, never switches its
    profile and never restarts it — switching is exactly what breaks the node.
    Returns (profile_uuid, wildcard_uuid).
    """
    if not config['bridge_address'] or not config['bridge_host_public_key']:
        raise ValueError('Мост не настроен: укажите адрес моста и проверьте подключение в настройках инфраструктуры')
    api = service.api
    node = await _bridge_infra_node(service, config)
    if node is None or not node.panel_uuid:
        raise ValueError('Бридж-нода не найдена: нет ноды, развёрнутой на адресе моста')
    panel_node = next((item for item in await api.nodes() if item.get('uuid') == node.panel_uuid), None)
    if not panel_node:
        raise ValueError('Бридж-нода не найдена в панели')
    profile_uuid = (panel_node.get('configProfile') or {}).get('activeConfigProfileUuid')
    if not profile_uuid:
        raise ValueError('Не удалось определить активный профиль бридж-ноды')
    profile = await read_profile(api, profile_uuid)
    wildcard_uuid = _wildcard_uuid(profile)
    if not wildcard_uuid:
        raise ValueError('В профиле нет inbound VLESS-WILDCARD; создайте его в панели')
    return profile_uuid, wildcard_uuid


async def _direct_host(api, node, bridge_address):
    """The node's own host, its source inbound and the active profile."""
    hosts = linked_hosts(await api.call('GET', '/hosts') or [], node.panel_uuid)
    candidates = [host for host in hosts if host.get('address') != bridge_address]
    direct = next((host for host in candidates if host['uuid'] == getattr(node, 'host_uuid', None)), None)
    if direct is None and len(candidates) == 1:
        direct = candidates[0]
    if not direct:
        raise ValueError('Не удалось однозначно выбрать прямой host ноды для моста')
    binding = direct.get('inbound') or {}
    panel_node = next((item for item in await api.nodes() if item.get('uuid') == node.panel_uuid), None)
    if not panel_node:
        raise ValueError('Нода не найдена в панели')
    profile_uuid = (panel_node.get('configProfile') or {}).get('activeConfigProfileUuid')
    source_id = binding.get('configProfileInboundUuid')
    if not profile_uuid or not source_id:
        raise ValueError('Не удалось определить текущий профиль ноды')
    if binding.get('configProfileUuid') != profile_uuid or source_id not in active_inbound_ids(panel_node):
        raise ValueError('Прямой host не использует активный inbound ноды')
    return direct, panel_node, profile_uuid, source_id


async def _gemini_enabled(service, node, host_uuids):
    async with service.factory() as session:
        for host_uuid in host_uuids:
            if host_uuid:
                _, saved = await read_gemini_state(service, session, node, host_uuid)
                if saved.get('enabled'):
                    return True
    return False


async def create_bridge_host(api, node, bridge_address, sni, binding, remark):
    """Create the panel host pointing at the bridge; returns (uuid, created)."""
    hosts = await api.call('GET', '/hosts') or []
    # One SNI is a single haproxy route: it must not be claimed by any other host.
    existing = next((host for host in hosts if host.get('sni') == sni
        and host.get('remark') == remark and host.get('address') == bridge_address
        and _host_nodes(host) == [node.panel_uuid]), None)
    if existing:
        if existing.get('inbound') != binding:
            raise ValueError('Существующий host моста использует другой inbound; синхронизируйте его в панели')
        return existing['uuid'], False
    duplicate = next((host for host in hosts if host.get('sni') == sni), None)
    if duplicate:
        raise ValueError('Этот SNI уже используется другим host')
    result = await api.call('POST', '/hosts', {'address': bridge_address, 'port': 443,
        'nodes': [node.panel_uuid], 'inbound': copy.deepcopy(binding), 'sni': sni, 'remark': remark})
    if not (result or {}).get('uuid'):
        raise RuntimeError('Панель не вернула созданный host моста')
    return result['uuid'], True


async def repoint_bridge_hosts(service, config):
    """Bridge hosts all point at the bridge address; update them after a change."""
    from db.dal import app_settings_dal
    address = (config['bridge_address'] or '').casefold()
    prefix = f'infra.bridge.{service.scope}.'
    async with service.factory() as session:
        values = await app_settings_dal.get_settings_map(session)
    hosts = await service.api.call('GET', '/hosts') or []
    updated = 0
    for key, value in values.items():
        if not key.startswith(prefix) or not value:
            continue
        try:
            state = json.loads(value)
        except (ValueError, TypeError):
            continue
        host_uuid = state.get('bridge_host_uuid')
        host = next((item for item in hosts if item.get('uuid') == host_uuid), None)
        if host and (host.get('address') or '').casefold() != address:
            await service.api.call('PATCH', '/hosts', dict(uuid=host_uuid,
                address=config['bridge_address']))
            updated += 1
    return updated


def _backend_address(address):
    """host:443 with brackets around an IPv6 address."""
    return f'[{address}]:443' if ':' in address else f'{address}:443'


async def _wildcard_mask_sni(service, config):
    """The wildcard's REALITY mask domain (its target/dest): haproxy routes it
    to the wildcard backend explicitly so the certificate SNI always reaches
    REALITY even if the default changes. Best-effort — absent or unreadable
    target simply adds no route."""
    try:
        node = await _bridge_infra_node(service, config)
        if node is None or not node.panel_uuid:
            return None
        panel_node = next((item for item in await service.api.nodes()
            if item.get('uuid') == node.panel_uuid), None)
        if not panel_node:
            return None
        profile_uuid = (panel_node.get('configProfile') or {}).get('activeConfigProfileUuid')
        if not profile_uuid:
            return None
        profile = await read_profile(service.api, profile_uuid)
        profile_config = profile.get('config')
        if isinstance(profile_config, str):
            profile_config = json.loads(profile_config)
        for entry in wildcard_variants(profile_config):
            reality = (entry.get('streamSettings') or {}).get('realitySettings') or {}
            target = str(reality.get('target') or reality.get('dest') or '')
            host, _, port = target.rpartition(':')
            if not port.isdigit():
                host = target
            if host:
                return validate_hostname(host.strip('[]'))
    except Exception:
        return None
    return None


async def desired_routes(service, config, exclude_node_id=None):
    """Every SNI route haproxy must serve: operator extras, one
    route per registered bridge (SNI → the bridged node's address:443 — the
    node terminates REALITY itself), and the wildcard's own mask domain routed
    to the local wildcard backend automatically. Unmatched SNIs fall through
    to the local wildcard, where REALITY masks them by proxying to the real
    site instead of fingerprinting an SNI-based reject."""
    from db.dal import app_settings_dal
    backend = validate_backend(config.get('bridge_wildcard_backend'))
    routes = []
    for entry in config.get('bridge_extra_routes') or []:
        sni, extra_backend = validate_route(entry)
        routes.append({'sni': sni, 'backend': extra_backend})
    prefix = f'infra.bridge.{service.scope}.'
    async with service.factory() as session:
        for key, value in (await app_settings_dal.get_settings_map(session)).items():
            if not key.startswith(prefix) or not value:
                continue
            node_id = key.rsplit('.', 1)[1]
            if exclude_node_id is not None and str(exclude_node_id) == node_id:
                continue
            try:
                state = json.loads(value)
            except (ValueError, TypeError):
                continue
            deployment = await service.deployment(session, int(node_id))
            if state.get('sni') and deployment:
                # A bridge forwards its SNI to the bridged node itself.
                routes.append({'sni': state['sni'], 'backend': _backend_address(deployment.address)})
    names = [route['sni'] for route in routes]
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f'Дублирующиеся SNI-маршруты: {", ".join(duplicates)}; проверьте настройки моста')
    # The mask domain rides along automatically unless already routed.
    mask = await _wildcard_mask_sni(service, config)
    if mask and mask not in names:
        routes.append({'sni': mask, 'backend': backend})
    return routes


async def create_bridge(service, node, deployment, config, job_id, sni):
    """Provision one bridge: switch the bridged node onto the wildcard listener
    (it must terminate REALITY with the wildcard keypair), render the routing
    table and create the host at the bridge address. The bridge node itself is
    never touched — haproxy owns its 443."""
    from .provisioning import ansible_bridge, stage

    if not sni:
        raise ValueError('Задание не содержит SNI моста')
    sni = validate_hostname(sni)
    if not config['bridge_address'] or not config['bridge_host_public_key']:
        raise ValueError('Мост не настроен: укажите адрес моста и проверьте подключение в настройках инфраструктуры')
    if not node.panel_uuid:
        raise ValueError('Нода не подключена к панели')
    api = service.api

    direct, panel_node, profile_uuid, source_id = await _direct_host(api, node, config['bridge_address'])
    profile = await read_profile(api, profile_uuid)
    wildcard_uuid = _wildcard_uuid(profile)
    if not wildcard_uuid:
        raise ValueError('В профиле нет inbound VLESS-WILDCARD; создайте его в панели')
    binding = wildcard_binding(profile_uuid, wildcard_uuid)
    profile_config = profile.get('config')
    if isinstance(profile_config, str):
        profile_config = json.loads(profile_config)
    sni_added = sni not in _wildcard_names(profile_config)
    hosts = await api.call('GET', '/hosts') or []
    existing_bridge = next((host for host in hosts if host.get('address') == config['bridge_address']
        and node.panel_uuid in _host_nodes(host)), None)
    if existing_bridge and existing_bridge.get('sni') != sni:
        raise ValueError('У этой ноды уже есть мост с другим SNI')
    if await _gemini_enabled(service, node, [direct['uuid']]):
        raise ValueError('Сначала отключите Gemini: прямой host будет переключён на wildcard')
    previous_inbounds = active_inbound_ids(panel_node)
    previous_host_inbound = copy.deepcopy(direct.get('inbound')) if direct.get('inbound') else None
    target_inbounds = list(dict.fromkeys(wildcard_uuid if identifier == source_id else identifier
        for identifier in previous_inbounds))
    # A pre-existing route or host (retry after a failed compensation) is left
    # intact: rollback only removes what this run actually created.
    route_existed = existing_bridge is not None
    await check_other_hosts(api, node.panel_uuid, direct['uuid'], profile_uuid, source_id,
        allowed_host_uuids=[existing_bridge['uuid']] if existing_bridge else [])
    routes = await desired_routes(service, config)
    route_in_table = any(route['sni'] == sni for route in routes)
    if not route_existed and route_in_table:
        raise ValueError(f'SNI {sni} уже используется другим маршрутом моста')
    remark = bridge_host_remark(node.country)

    # The wildcard accepts the SNI and the node serves it before haproxy
    # starts forwarding the SNI to the node.
    changed_squads = []
    node_switched = host_switched = False
    host_uuid = None
    host_created = False
    try:
        await stage(service, job_id, 'bridge_profile')
        if sni_added:
            await add_profile_sni(api, profile_uuid, sni)
        changed_squads = await add_inbound_to_squads(api, source_id, wildcard_uuid)
        await assert_selection_unchanged(api, panel_node, direct)
        if previous_inbounds != target_inbounds:
            node_switched = True
            await switch_profile(api, node.panel_uuid, profile_uuid, target_inbounds)
        if previous_host_inbound != binding:
            host_switched = True
            await api.call('PATCH', '/hosts', dict(uuid=direct['uuid'],
                inbound=copy.deepcopy(binding)))
        await api.restart_node(node.panel_uuid, force=True)
        await verify_connected(api, node.panel_uuid)
        await stage(service, job_id, 'bridge')
        if not route_in_table:
            # A fresh route — or a retry with an orphaned host whose
            # registration never saved: haproxy must serve the SNI either way.
            routes.append({'sni': sni, 'backend': _backend_address(deployment.address)})
        await ansible_bridge(service, config, routes)
        await stage(service, job_id, 'bridge_host')
        host_uuid, host_created = await create_bridge_host(api, node, config['bridge_address'], sni, binding, remark)
        hosts = await api.call('GET', '/hosts') or []
        verified = next((host for host in hosts if host.get('uuid') == host_uuid
            and host.get('sni') == sni and not host.get('isDisabled')
            and host.get('inbound') == binding
            and host.get('address') == config['bridge_address']
            and _host_nodes(host) == [node.panel_uuid]), None)
        if not verified:
            raise RuntimeError('Панель не подтвердила созданный host моста')
        async with service.factory() as session:
            from db.dal import app_settings_dal
            row = await session.get(Deployment, deployment.id)
            if row:
                row.profile_uuid, row.inbound_uuid = profile_uuid, wildcard_uuid
            await app_settings_dal.upsert_setting(session, f'infra.bridge.{service.scope}.{node.id}', json.dumps({
                'direct_host_uuid': direct['uuid'], 'bridge_host_uuid': host_uuid, 'sni': sni,
                'sni_added': sni_added, 'profile_uuid': profile_uuid, 'wildcard_uuid': wildcard_uuid,
                'source_inbound_uuid': source_id, 'previous_inbounds': previous_inbounds,
                'previous_host_inbound': previous_host_inbound, 'changed_squads': changed_squads,
                'previous_deployment': {'profile_uuid': deployment.profile_uuid,
                                        'inbound_uuid': deployment.inbound_uuid}}))
            await session.commit()
    except BaseException:
        # Compensate in reverse order; a partial bridge must not stay behind.
        errors = []
        if host_uuid and host_created:
            try:
                await api.call('DELETE', f'/hosts/{host_uuid}')
            except Exception as exc:
                errors.append(exc)
        if not route_existed:
            try:
                # Re-render without this route: the registration was never saved.
                await ansible_bridge(service, config, await desired_routes(service, config))
            except Exception as exc:
                errors.append(exc)
        if host_switched and previous_host_inbound:
            try:
                await api.call('PATCH', '/hosts', dict(uuid=direct['uuid'],
                    inbound=copy.deepcopy(previous_host_inbound)))
            except Exception as exc:
                errors.append(exc)
        if node_switched:
            try:
                await switch_profile(api, node.panel_uuid, profile_uuid, previous_inbounds)
                await api.restart_node(node.panel_uuid, force=True)
                await verify_connected(api, node.panel_uuid)
            except Exception as exc:
                errors.append(exc)
        try:
            await restore_squads(api, changed_squads, source_id, wildcard_uuid)
        except Exception as exc:
            errors.append(exc)
        if sni_added and not route_existed:
            # A healed retry keeps its pre-existing host and route, so its SNI
            # stays on the wildcard as well.
            try:
                await remove_profile_sni(api, profile_uuid, sni)
            except Exception as exc:
                errors.append(exc)
        if errors:
            # Own error texts are safe (ansible output is sanitized upstream);
            # surface them so the admin sees the actual failed step.
            details = '; '.join(f'{type(exc).__name__}: {exc}' for exc in errors[:3])[:400]
            raise RuntimeError(f'Создание моста не завершилось, автоматический откат требует '
                               f'проверки: {details}') from errors[0]
        raise


async def read_bridge_state(service, session, node):
    from db.dal import app_settings_dal
    row = await app_settings_dal.get_setting(session, f'infra.bridge.{service.scope}.{node.id}')
    return json.loads(row.value) if row and row.value else {}


async def bridge_entries(service, session):
    """Registered bridges as [(node_id, node_name, sni)] sorted by node name."""
    from db.dal import app_settings_dal
    prefix = f'infra.bridge.{service.scope}.'
    values = await app_settings_dal.get_settings_map(session)
    entries = []
    for key, value in values.items():
        if not key.startswith(prefix) or not value:
            continue
        try:
            node = await service.node(session, key.rsplit('.', 1)[1])
            state = json.loads(value)
        except (ValueError, TypeError):
            continue
        if state.get('sni'):
            entries.append((node.id, node.name, state['sni']))
    return sorted(entries, key=lambda item: item[1])


async def _other_registrations(service, node):
    """Bridge states of other nodes; their SNIs must survive removal."""
    from db.dal import app_settings_dal
    prefix = f'infra.bridge.{service.scope}.'
    async with service.factory() as session:
        values = await app_settings_dal.get_settings_map(session)
    others = []
    for key, value in values.items():
        if not key.startswith(prefix) or not value or key == f'{prefix}{node.id}':
            continue
        try:
            others.append(json.loads(value))
        except (ValueError, TypeError):
            continue
    return others


async def remove_bridge(service, node, deployment, config, job_id):
    """Remove the node's bridge, undoing the recorded creation steps in reverse:
    the host, the route (re-render without this bridge), the direct-host
    binding, the node's inbounds, squad memberships and — only when nothing
    else uses it — the wildcard SNI. The bridge node is never touched."""
    from db.dal import app_settings_dal
    from .provisioning import ansible_bridge, stage

    if not config['bridge_address'] or not config['bridge_host_public_key']:
        raise ValueError('Мост не настроен: укажите адрес моста и проверьте подключение в настройках инфраструктуры')
    api = service.api
    async with service.factory() as session:
        state = await read_bridge_state(service, session, node)
        gemini_enabled = False
        for host_uuid in (state.get('direct_host_uuid'), state.get('bridge_host_uuid')):
            if host_uuid:
                _, saved = await read_gemini_state(service, session, node, host_uuid)
                gemini_enabled = gemini_enabled or bool(saved.get('enabled'))
    if not state.get('sni'):
        raise ValueError('Для этой ноды нет зарегистрированного моста')
    sni = validate_hostname(state['sni'])
    if gemini_enabled:
        raise ValueError('Сначала отключите Gemini: мост и прямой host переключаются вместе')

    hosts = await api.call('GET', '/hosts') or []
    bridge_host = next((host for host in hosts if host['uuid'] == state.get('bridge_host_uuid')), None)
    direct = next((host for host in hosts if host['uuid'] == state.get('direct_host_uuid')), None)
    panel_node = next((item for item in await api.nodes() if item.get('uuid') == node.panel_uuid), None)
    if not panel_node:
        raise ValueError('Нода не найдена в панели')
    profile_uuid = state.get('profile_uuid') or (panel_node.get('configProfile') or {}).get('activeConfigProfileUuid')
    if not profile_uuid:
        raise ValueError('Не удалось определить профиль wildcard')
    profile = await read_profile(api, profile_uuid)
    wildcard_uuid = state.get('wildcard_uuid') or _wildcard_uuid(profile)
    if not wildcard_uuid:
        raise ValueError('В профиле нет inbound VLESS-WILDCARD')
    previous_inbounds = state.get('previous_inbounds')
    previous_host_inbound = state.get('previous_host_inbound')
    previous_deployment = state.get('previous_deployment') or {}
    others = await _other_registrations(service, node)
    extras = {validate_route(entry)[0] for entry in config.get('bridge_extra_routes') or []}
    excluded = {(bridge_host or {}).get('uuid'), (direct or {}).get('uuid')} - {None}
    sni_in_use = sni in extras or any(item.get('sni') == sni for item in others) or any(
        host.get('sni') == sni and host['uuid'] not in excluded for host in hosts)

    errors = []
    await stage(service, job_id, 'bridge_host')
    if bridge_host:
        try:
            await api.call('DELETE', f'/hosts/{bridge_host["uuid"]}')
        except Exception as exc:
            errors.append(exc)
    await stage(service, job_id, 'bridge')
    try:
        # The registration is still recorded; render everything except this node.
        await ansible_bridge(service, config, await desired_routes(service, config, exclude_node_id=node.id))
    except Exception as exc:
        errors.append(exc)
    await stage(service, job_id, 'bridge_profile')
    if previous_host_inbound and direct and (direct.get('inbound') or {}) != previous_host_inbound:
        try:
            await api.call('PATCH', '/hosts', dict(uuid=direct['uuid'],
                inbound=copy.deepcopy(previous_host_inbound)))
        except Exception as exc:
            errors.append(exc)
    if previous_inbounds and set(active_inbound_ids(panel_node)) != set(previous_inbounds):
        try:
            await switch_profile(api, node.panel_uuid, profile_uuid, previous_inbounds)
            await api.restart_node(node.panel_uuid, force=True)
            await verify_connected(api, node.panel_uuid)
        except Exception as exc:
            errors.append(exc)
    if not errors:
        # Restore only the squad memberships creation added.
        try:
            await restore_squads(api, state.get('changed_squads') or [],
                state.get('source_inbound_uuid'), wildcard_uuid)
        except Exception as exc:
            errors.append(exc)
    if not errors and state.get('sni_added', True) and not sni_in_use:
        # After the re-render nobody routes this SNI any more: drop it from
        # the wildcard list. Extras and other bridges keep theirs.
        try:
            await remove_profile_sni(api, profile_uuid, sni)
        except Exception as exc:
            errors.append(exc)
    if errors:
        # Keep the registration so the removal can be retried after fixing.
        details = '; '.join(f'{type(exc).__name__}: {exc}' for exc in errors[:3])[:400]
        raise RuntimeError(f'Удаление моста не завершилось, требуется проверка панели: {details}') from errors[0]
    async with service.factory() as session:
        row = await session.get(Deployment, deployment.id)
        if row and previous_deployment.get('profile_uuid'):
            row.profile_uuid = previous_deployment['profile_uuid']
            row.inbound_uuid = previous_deployment.get('inbound_uuid')
        await app_settings_dal.delete_setting(session, f'infra.bridge.{service.scope}.{node.id}')
        if state.get('bridge_host_uuid'):
            # The bridge host is gone; its companion state would be orphaned.
            await app_settings_dal.delete_setting(session,
                f'infra.gemini.{service.scope}.{node.id}.{state["bridge_host_uuid"]}')
        await session.commit()


def _certificate_name_covers(sni, name):
    """TLS wildcard semantics: an entry like *.example.com covers exactly one
    leftmost label, never a sub-sub-domain."""
    sni = sni.casefold()
    name = name.casefold()
    if name.startswith('*.'):
        base = name[2:]
        if not sni.endswith('.' + base):
            return False
        return '.' not in sni[:-(len(base) + 1)]
    return sni == name


async def _sni_supported(host, sni, timeout=10):
    """Whether the TLS certificate served by host:443 covers sni (SAN or CN,
    with wildcard entries matched the TLS way)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 443, ssl=context,
        server_name=sni), timeout)
    try:
        der = writer.getpeercert(binary_form=True)
        certificate = x509.load_der_x509_certificate(der)
        names = set()
        try:
            extension = certificate.extensions.get_extension_for_oid(
                ExtensionOID.CERTIFICATE_SUBJECT_ALTERNATIVE_NAME)
            names.update(extension.value.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            pass
        for attribute in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
            names.add(attribute.value)
        return any(_certificate_name_covers(sni, name) for name in names)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def change_wildcard_dest(service, config, domain):
    """Re-mask the wildcard inbound onto domain after verifying that domain's
    certificate covers every SNI the routing table serves — both the SNIs
    already listed on the wildcard and the ones a pending apply would add.
    Writes the domain into REALITY target and serverNames, then renders
    haproxy so the domain is routed to the local wildcard backend. The bridge
    node is not restarted. Returns the number of verified SNIs."""
    from .provisioning import ansible_bridge

    domain = validate_hostname(domain)
    profile_uuid, _ = await ensure_bridge_wildcard(service, config)
    api = service.api
    profile = await read_profile(api, profile_uuid)
    profile_config = profile.get('config')
    if isinstance(profile_config, str):
        profile_config = json.loads(profile_config)
    variants = wildcard_variants(profile_config)
    if not variants:
        raise ValueError('В профиле нет inbound VLESS-WILDCARD')
    table = {route['sni'] for route in await desired_routes(service, config)}
    names = sorted(_wildcard_names(profile_config) | table)
    failed = []
    for sni in names:
        try:
            if not await _sni_supported(domain, sni):
                failed.append(sni)
        except Exception:
            failed.append(sni)
    if failed:
        raise ValueError(f'Сертификат {domain} не покрывает: {", ".join(failed)}; смена wildcard отменена')
    target = f'{domain}:443'
    for entry in variants:
        reality = (entry.get('streamSettings') or {}).get('realitySettings') or {}
        if 'target' in reality:
            reality['target'] = target
        elif 'dest' in reality:
            reality['dest'] = target
        else:
            reality['target'] = target
        listed = list(reality.get('serverNames') or [])
        if domain not in listed:
            reality['serverNames'] = listed + [domain]
        entry['streamSettings']['realitySettings'] = reality
    await api.call('PATCH', '/config-profiles', {'uuid': profile_uuid, 'config': profile_config})
    # The mask route derives from the new target: render it onto the bridge.
    await ansible_bridge(service, config, await desired_routes(service, config))
    return len(names)
