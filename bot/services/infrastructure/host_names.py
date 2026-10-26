"""Names for client-facing hosts and recovery of interrupted creation."""
import re

import pycountry


def country_host_base(code):
    code = (code or '').upper()
    country = pycountry.countries.get(alpha_2=code)
    if not country:
        return code or 'Unknown'
    name = 'Russia' if code == 'RU' else getattr(country, 'common_name', country.name)
    flag = ''.join(chr(127397 + ord(letter)) for letter in code)
    return f'{flag} {name}'


def country_host_name(code, hosts, nodes):
    base = country_host_base(code)
    pattern = re.compile(rf'^{re.escape(base)}(?: ([2-9][0-9]*|1[0-9]+))?$', re.IGNORECASE)
    node_countries = {node['uuid']: (node.get('countryCode') or '').upper()
                      for node in nodes if node.get('uuid')}
    used_numbers = []
    for host in hosts:
        match = pattern.fullmatch(host.get('remark') or '')
        node_ids = [node.get('uuid') if isinstance(node, dict) else node
                    for node in host.get('nodes') or []]
        same_country = any(node_countries.get(node_id) == code.upper() for node_id in node_ids)
        if match or same_country:
            used_numbers.append(int(match.group(1)) if match and match.group(1) else 1)
    if not used_numbers:
        return base
    return f'{base} {max(max(used_numbers), len(used_numbers)) + 1}'


def existing_created_host(hosts, node, deployment):
    """Recognize the host after a create succeeded but its response was lost."""
    base = country_host_base(node.country)
    pattern = re.compile(rf'^{re.escape(base)}(?: ([2-9][0-9]*|1[0-9]+))?$', re.IGNORECASE)
    candidates = [host for host in hosts if host.get('nodes') == [node.panel_uuid]
        and host.get('address') == deployment.host_address
        and (host.get('remark') == node.name or pattern.fullmatch(host.get('remark') or ''))]
    if len(candidates) > 1:
        raise ValueError('Найдено несколько подходящих hosts. Проверьте hosts ноды в панели.')
    return candidates[0] if candidates else None
