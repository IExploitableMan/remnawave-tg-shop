"""Conservative adapters for diagnostic tables; original output stays in JSON."""
import re
import json
from collections import Counter

SPEED = re.compile(r'([0-9]+(?:\.[0-9]+)?)\s*(Mbps|[MG]bits/sec|[MG]bit/s|[MG]B/s)\b')


def columns(line):
    return [part.strip() for part in re.split(r'\s{2,}|\t+|\s*\|\s*', line.strip()) if part.strip()]


def service_status(value):
    if re.search(r'\bblock(?:ed)?\b|denied|failed|banned|restricted|\bno\b|timeout|reset', value, re.I):
        return 'bad'
    if re.search(r'VeryHigh|HighRisk|Suspicious|Risky', value, re.I):
        return 'bad'
    if re.search(r'NF.?Only|No.?Prem|discrepant|high|medium|elevated', value, re.I):
        return 'warn'
    if value.strip() in ('N/A', '-', '?', 'Unknown'):
        return 'unknown'
    return 'ok'


def parse_result(result):
    output = result['output']
    kind = result.get('id', '')
    facts, services, bars = [], [], []
    lines = output.splitlines()
    families, family = [], ''
    for line in lines:
        if 'IP QUALITY CHECK REPORT:' in line:
            address = line.split('REPORT:', 1)[1].strip()
            family = 'IPv6' if ':' in address else 'IPv4'
        families.append(family)
    # System and benchmark headings use stable key:value labels.
    keys = {'Processor': 'CPU', 'CPU Model': 'CPU', 'CPU cores': 'Ядер', 'CPU Cores': 'Ядер',
            'RAM': 'RAM', 'Total RAM': 'RAM', 'Disk': 'Диск', 'Total Disk': 'Диск',
            'TCP Congestion Control': 'CC', 'I/O Speed(average)': 'I/O', 'ASN': 'ASN',
            'Organization': 'Сеть', 'events per second': 'CPU events/s',
            'Single Core': 'Geekbench single', 'Multi Core': 'Geekbench multi'}
    for offset, line in enumerate(lines):
        match = re.match(r'^\s*([^:]+):\s*(.+?)\s*$', line)
        if match and match[1].strip() in keys:
            label = keys[match[1].strip()]
            facts.append((f'{label} {families[offset]}'.strip(), match[2]))
    if kind == 'ipregion':
        for line in lines:
            if line.strip() == 'Legend':
                break
            row = columns(line)
            if (len(row) >= 2 and len(row[0]) <= 35 and re.match(r'^[A-Za-z0-9]', row[0])
                    and not re.search(r'[:/\\]|checking|made with', row[0], re.I)
                    and row[0] not in ('Service', 'Code', 'Country', 'IPv4', 'IPv6')
                    and re.match(r'^(?:[A-Z]{2}\b|Yes\b|No\b|N/A|Blocked|Available|Unknown)', row[1])):
                value = ' / '.join(row[1:3])
                status = service_status(value)
                if 'Search Captcha' in row[0]:
                    status = 'ok' if row[1] == 'No' else ('bad' if row[1] == 'Yes' else 'unknown')
                services.append({'label': row[0], 'value': value, 'status': status})
        regions = Counter(s['value'].split(' / ')[0] for s in services if re.fullmatch(r'[A-Z]{2}', s['value'].split(' / ')[0]))
        if regions:
            country, count = regions.most_common(1)[0]
            facts += [('Консенсус', country), ('Совпадений', f'{count}/{sum(regions.values())}')]
    elif kind in ('ipcheck', 'ipquality'):
        # Multiple matrices may appear; process each Service row independently.
        for offset, line in enumerate(lines):
            if not re.match(r'^\s*Service:', line):
                continue
            header = line.split(':', 1)[1]
            matches = list(re.finditer(r'\S+', header))
            names = [m[0] for m in matches]
            centers = [(m.start()+m.end())/2 for m in matches]
            boundaries = [0] + [int((a+b)/2) for a,b in zip(centers, centers[1:])] + [max(len(header), 1000)]
            status, regions = [], []
            for following in lines[offset+1:offset+7]:
                if re.match(r'^\s*Status:', following):
                    status = following.split(':', 1)[1].split()
                elif re.match(r'^\s*Region:', following):
                    body = following.split(':', 1)[1]
                    regions = [body[left:right].strip() for left,right in zip(boundaries,boundaries[1:])]
            for index, name in enumerate(names):
                value = status[index] if index < len(status) else 'N/A'
                region = regions[index] if index < len(regions) else ''
                label = f'{name} [{families[offset]}]' if families[offset] else name
                services.append({'label': label, 'value': ' · '.join(v for v in (value, region) if v and v != '-'),
                                 'status': service_status(value)})
        for offset, line in enumerate(lines):
            match = re.match(r'^\s*(IP2Location|Scamalytics|ipapi|AbuseIPDB|DB-?IP|IPQS):\s*(.*)', line)
            if match and re.search(r'\d|Low|High|Suspicious', match[2]):
                label = f'{match[1]} [{families[offset]}]' if families[offset] else match[1]
                services.append({'label': label, 'value': match[2].strip().replace('|', ' · '), 'status': service_status(match[2])})
            match = re.match(r'^\s*(Usage|Company|Proxy|VPN|Tor):\s*(.*)', line)
            if match:
                values = columns(match[2])
                if values:
                    facts.append((f'{match[1]} {families[offset]}'.strip(), Counter(values).most_common(1)[0][0]))
    elif kind in ('geo', 'dpi', 'tlab_censor'):
        for line in lines:
            match = re.match(r'^\s*([a-z0-9.-]+\.[a-z]{2,})\s+(.+)', line, re.I)
            if match and re.search(r'available|blocked|denied|timeout|reset|\bOK\b', match[2], re.I):
                services.append({'label': match[1], 'value': match[2].strip(), 'status': service_status(match[2])})
    if kind in ('ru_speed', 'tlab_speed', 'yabs', 'bench'):
        family = ''
        for line in lines:
            if 'Network Speed Tests (IPv' in line:
                family = 'IPv6' if '(IPv6)' in line else 'IPv4'
            speeds = list(SPEED.finditer(line))
            if not speeds and re.search(r'busy|failed|N/A', line, re.I):
                row = columns(line)
                if len(row) >= 3 and row[0] not in ('Provider', 'Node Name'):
                    services.append({'label': row[0], 'value': 'Нет замера', 'status': 'unknown'})
            if not speeds or (kind == 'yabs' and '|' not in line) or 'Total' in line:
                continue
            label = line[:speeds[0].start()].strip(' |')
            if not label or re.search(r'I/O|average|read|write|Block Size', label, re.I):
                continue
            numbers = [float(m[1]) * (1000 if m[2] in ('Gbits/sec', 'Gbit/s') else 1) for m in speeds]
            # YABS/bench/tlab send/receive; itdog's Russian table down/up.
            reverse = kind in ('yabs', 'bench', 'tlab_speed')
            download = numbers[1] if reverse and len(numbers) > 1 else numbers[0]
            upload = numbers[0] if reverse else (numbers[1] if len(numbers) > 1 else None)
            label = re.sub(r'\s*\|\s*', ' · ', label)
            if family:
                label = f'{label} [{family}]'
            bars.append({'label': label, 'value': download,
                         'unit': 'Mbps', 'upload': upload})
        if bars:
            facts += [('Макс ↓', f"{max(b['value'] for b in bars):.1f} Mbps"), ('Направлений', str(len(bars)))]
    return {'facts': list(dict.fromkeys(facts)), 'services': services, 'bars': bars}


def result_measurements(result):
    data = parse_result(result)
    metrics = {}
    for row in data['bars']:
        metrics[f"{row['label']} download [Mbps]"] = row['value']
        if row['upload'] is not None:
            metrics[f"{row['label']} upload [Mbps]"] = row['upload']
    for label, value in data['facts']:
        if label in ('CPU events/s', 'Geekbench single', 'Geekbench multi'):
            number = re.search(r'\d+(?:\.\d+)?', value)
            if number:
                metrics[label] = float(number[0])
    if result.get('id') in ('udp', 'sustained'):
        try:
            raw = json.loads(result['output'])
            for direction in ('sum', 'sum_sent', 'sum_received'):
                values = raw.get('end', {}).get(direction, {})
                for key, unit, scale in [('bits_per_second', 'Mbps', 1000000),
                                         ('lost_percent', '%', 1), ('jitter_ms', 'ms', 1)]:
                    if isinstance(values.get(key), (int, float)):
                        metrics[f'{direction} {key} [{unit}]'] = values[key]/scale
        except (ValueError, TypeError):
            pass
    if result.get('id') == 'memory':
        match = re.search(r'\(([0-9.]+) MiB/sec\)', result['output'])
        if match:
            metrics['Memory [MiB/sec]'] = float(match[1])
    return metrics
