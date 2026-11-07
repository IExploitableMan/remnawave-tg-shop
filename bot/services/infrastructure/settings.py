import ipaddress
import copy
import json
import re
from db.dal import app_settings_dal

DEFAULTS = {
    'default_days': 30, 'warning_hours': [72, 24], 'expire_exclude_tg': False,
    'profile_uuid': '', 'inbound_uuid': '', 'tg_squad_uuid': '', 'panel_ip': '',
    'node_version_2': '2.8.0', 'node_version_3': '3.4.2',
    'sni_ru': [], 'sni_other': [], 'ssh_keys': [], 'large_repeats': 1,
    'bridge_address': '', 'bridge_ssh_port': 22, 'bridge_ssh_user': 'root',
    'bridge_host_public_key': '', 'bridge_extra_routes': [],
    'bridge_wildcard_backend': '127.0.0.1:8443',
}


def next_node_number(names):
    """Use only current node names, including bare node2; ignore archived names."""
    maximum = max([int(match[1]) for name in names
        if (match := re.match(r'^node(\d+)(?:\b|\s)', name.strip(), re.I))] or [0])
    return maximum + 1


def validate_hostname(value):
    value = str(value).strip().lower().rstrip('.')
    if not value or len(value) > 253 or not all(re.fullmatch(
            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.')):
        raise ValueError('Некорректный домен')
    return value


def validate_address(value):
    value = str(value).strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return validate_hostname(value)


def validate_route(value):
    """'sni host:port' with an optional bracketed IPv6 host; returns (sni, backend)."""
    parts = str(value).split()
    if len(parts) != 2:
        raise ValueError('Некорректный маршрут: ожидается "sni host:port"')
    sni = validate_hostname(parts[0])
    host, separator, port = parts[1].rpartition(':')
    host = host.strip('[]')
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError('Некорректный маршрут: ожидается "sni host:port"') from None
    if not separator or not 1 <= port <= 65535 or not host:
        raise ValueError('Некорректный маршрут: ожидается "sni host:port"')
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        host = validate_hostname(host)
    # haproxy requires brackets around an IPv6 backend address.
    return sni, f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


def validate_backend(value):
    """'host:port' with an optional bracketed IPv6 host; returns 'host:port'."""
    return validate_route(f'sni.invalid {value}')[1]


async def read_config(session):
    row = await app_settings_dal.get_setting(session, 'infra.config')
    return dict(copy.deepcopy(DEFAULTS), **(json.loads(row.value) if row else {}))


async def save_config(session, key, value, admin_id):
    if key not in DEFAULTS:
        raise ValueError('Неизвестная настройка')
    from sqlalchemy import text
    await session.execute(text('SELECT pg_advisory_xact_lock(74120303)'))
    config = await read_config(session)
    config[key] = value
    await app_settings_dal.upsert_setting(session, 'infra.config', json.dumps(config), updated_by=admin_id)


def choose_sni(pool, deployments):
    if not pool:
        return None
    import secrets
    counts = {s: sum(d.sni == s for d in deployments) for s in pool}
    minimum = min(counts.values())
    return secrets.choice([s for s in pool if counts[s] == minimum])
