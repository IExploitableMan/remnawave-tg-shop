"""haproxy SNI fronting bridges on the shared bridge server."""
import copy

from db.models import InfrastructureDeployment as Deployment
from .gemini import parse_config, read_profile, switch_profile, _panel_node_host
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


async def add_profile_sni(api, profile_uuid, sni):
    """Append the bridge SNI to serverNames of the VLESS-WILDCARD inbound.

    The profile must accept the SNI before haproxy starts forwarding it."""
    profile, config, reality = await _wildcard_state(api, profile_uuid)
    names = list(reality.get('serverNames') or [])
    if sni in names:
        return False
    reality['serverNames'] = names + [sni]
    await api.call('PATCH', f"/config-profiles/{profile_uuid}", {'config': config})
    _, _, verified = await _wildcard_state(api, profile_uuid)
    if sni not in (verified.get('serverNames') or []):
        raise RuntimeError('Панель не подтвердила добавление SNI в профиль')
    return True


async def remove_profile_sni(api, profile_uuid, sni):
    """Compensation step: drop the bridge SNI from serverNames again."""
    profile, config, reality = await _wildcard_state(api, profile_uuid)
    names = list(reality.get('serverNames') or [])
    if sni not in names:
        return False
    reality['serverNames'] = [item for item in names if item != sni]
    await api.call('PATCH', f"/config-profiles/{profile_uuid}", {'config': config})
    return True


async def create_bridge_host(api, node, bridge_address, sni, profile_uuid, inbound_uuid):
    """Create the panel host pointing at the bridge; returns (uuid, created)."""
    hosts = await api.call('GET', '/hosts') or []
    remark = bridge_host_remark(node.name)
    # One SNI is a single haproxy route: it must not be claimed by any other host.
    existing = next((host for host in hosts if host.get('sni') == sni
        and host.get('remark') == remark and host.get('address') == bridge_address
        and _host_nodes(host) == [node.panel_uuid]), None)
    if existing:
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

    # The wildcard inbound must exist in the profile the node currently uses;
    # the node keeps exactly one active inbound: wildcard replaces VLESS-443.
    panel_node, direct_host = await _panel_node_host(api, node.panel_uuid)
    cp = panel_node.get('configProfile') or {}
    wildcard_profile = cp.get('activeConfigProfileUuid')
    previous_inbounds = [item.get('uuid') if isinstance(item, dict) else item
                         for item in cp.get('activeInbounds') or []]
    if not wildcard_profile or not previous_inbounds:
        raise ValueError('Не удалось определить текущий профиль ноды')
    profile = await read_profile(api, wildcard_profile)
    wildcard_uuid = _wildcard_uuid(profile)
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
    target = f'[{deployment.address}]' if ':' in deployment.address else deployment.address
    backend = f'{target}:443'

    # The profile accepts the SNI and the node runs only the wildcard inbound
    # before haproxy starts forwarding the SNI to it.
    profile_changed = False
    profile_uncertain = False  # add_profile_sni may apply its PATCH before failing
    node_switched = host_switched = bridge_added = False
    host_uuid = None
    host_created = False
    try:
        await stage(service, job_id, 'bridge_profile')
        try:
            profile_changed = await add_profile_sni(api, wildcard_profile, sni)
        except BaseException:
            profile_uncertain = True
            raise
        if previous_inbounds != [wildcard_uuid]:
            await switch_profile(api, node.panel_uuid, wildcard_profile, [wildcard_uuid])
            node_switched = True
        if previous_host_inbound != wildcard_host_inbound:
            await api.call('PATCH', '/hosts', dict(uuid=direct_host['uuid'],
                inbound=copy.deepcopy(wildcard_host_inbound)))
            host_switched = True
        await stage(service, job_id, 'bridge')
        await ansible_bridge(service, config, sni, backend)
        bridge_added = not route_existed
        await stage(service, job_id, 'bridge_host')
        host_uuid, host_created = await create_bridge_host(api, node, config['bridge_address'], sni,
                                                           wildcard_profile, wildcard_uuid)
        hosts = await api.call('GET', '/hosts') or []
        verified = next((host for host in hosts if host.get('uuid') == host_uuid
            and host.get('sni') == sni and not host.get('isDisabled')), None)
        if not verified:
            raise RuntimeError('Панель не подтвердила созданный host моста')
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
            except Exception as exc:
                rollback_errors.append(exc)
        if profile_changed or profile_uncertain:
            try:
                await remove_profile_sni(api, wildcard_profile, sni)
            except Exception as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            raise RuntimeError('Создание моста не завершилось, автоматический откат требует проверки') from rollback_errors[0]
        raise
    # The wildcard inbound is now what the node serves; keep the card in sync.
    async with service.factory() as session:
        row = await session.get(Deployment, deployment.id)
        row.profile_uuid, row.inbound_uuid = wildcard_profile, wildcard_uuid
        await session.commit()
