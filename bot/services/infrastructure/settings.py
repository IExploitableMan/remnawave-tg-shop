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
