"""Compact views of structured diagnostics. Raw results remain in checkpoints."""
import json
import re
import statistics


GREEN, BLUE, RED, RESET = '\x1b[32m', '\x1b[34m', '\x1b[31m', '\x1b[0m'


def number(value, suffix='', scale=1):
    return f'{value / scale:.2f}{suffix}' if isinstance(value, (int, float)) else 'N/A'


def iperf_summary(result):
    try:
        raw = json.loads(result['output'])
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    lines = []
    if raw.get('error'):
        lines += [RED + 'Error: ' + str(raw['error']) + RESET, '']
    start = raw.get('start') or {}
    test = start.get('test_start') or {}
    connected = start.get('connected') or []
    if connected:
        endpoint = connected[0]
        lines.append(f"Remote endpoint: {endpoint.get('remote_host', 'N/A')}:{endpoint.get('remote_port', 'N/A')}")
    if test:
        direction = 'remote → node' if test.get('reverse') else 'node → remote'
        lines += [f"Protocol: {test.get('protocol', 'N/A')}  |  Direction: {direction}",
                  f"Requested duration: {test.get('duration', 'N/A')} s", '']
    end = raw.get('end') or {}
    for key, label in (('sum_sent', 'Sender'), ('sum_received', 'Receiver'), ('sum', 'Summary')):
        values = end.get(key)
        if not isinstance(values, dict):
            continue
        if key == 'sum' and (end.get('sum_sent') or end.get('sum_received')):
            continue
        lines += [BLUE + label + RESET,
                  f"  Measured speed : {GREEN}{number(values.get('bits_per_second'), ' Mbps', 1_000_000)}{RESET}",
                  f"  Measured time  : {number(values.get('seconds'), ' s')}",
                  f"  Transferred    : {number(values.get('bytes'), ' MiB', 1024**2)}"]
        if result.get('id') == 'udp':
            lines += [f"  Packet loss    : {number(values.get('lost_percent'), '%')} "
                      f"({values.get('lost_packets', 'N/A')} / {values.get('packets', 'N/A')} packets)",
                      f"  Jitter         : {number(values.get('jitter_ms'), ' ms')}"]
        if 'retransmits' in values:
            lines.append(f"  Retransmits    : {values['retransmits']}")
        lines.append('')
    if result.get('id') == 'sustained':
        intervals = []
        for interval in raw.get('intervals') or []:
            values = interval.get('sum') or interval.get('sum_sent') or {}
            rate = values.get('bits_per_second')
            if isinstance(rate, (int, float)):
                intervals.append((values.get('start', 0), values.get('end', 0), rate / 1_000_000))
        if intervals:
            lines += [BLUE + 'Intervals' + RESET, 'Time (s)           Speed (Mbps)']
            for begin, finish, rate in intervals:
                lines.append(f'{begin:6.1f}–{finish:<6.1f}   {rate:12.2f}')
            values = [r[2] for r in intervals]
            lines += ['', f'Interval min / median / max: {min(values):.2f} / {statistics.median(values):.2f} / {max(values):.2f} Mbps']
    if not lines:
        return RED + 'No usable measurements in saved iperf output' + RESET
    return '\n'.join(lines)


def dns_summary(result):
    groups, current = [], None
    for line in result['output'].splitlines():
        if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', line.strip()):
            current = {'resolver': line.strip(), 'status': 'N/A', 'time': 'N/A', 'errors': []}
            groups.append(current)
        if current is None:
            continue
        status = re.search(r'status:\s*([A-Z]+)', line)
        query = re.search(r'Query time:\s*(\d+)\s*msec', line)
        if status: current['status'] = status[1]
        if query: current['time'] = query[1] + ' ms'
        if re.search(r'timed out|no servers|communications error|could not|failed', line, re.I):
            current['errors'].append(line.strip())
    if not groups:
        return None
    lines = [BLUE + f'{"Resolver":<18}{"Status":<16}Query time' + RESET]
    for row in groups:
        ink = GREEN if row['status'] == 'NOERROR' and not row['errors'] else RED
        lines.append(f"{row['resolver']:<18}{ink}{row['status']:<16}{RESET}{row['time']}")
        lines += [RED + '  ' + error + RESET for error in dict.fromkeys(row['errors'])]
    return '\n'.join(lines)


def cdn_summary(result):
    lines, found = [BLUE + f'{"Endpoint":<42}{"Connect ms":>12}{"TLS ms":>12}{"TTFB ms":>12}' + RESET], False
    for line in result['output'].splitlines():
        values = dict(re.findall(r'(connect|tls|ttfb)=([0-9.]+)', line))
        if values:
            found = True
            endpoint = line.split()[0]
            times = [float(values.get(k, 0)) * 1000 for k in ('connect', 'tls', 'ttfb')]
            if times[-1] <= 0:
                lines.append(RED + endpoint + '  failed / no response' + RESET)
            else:
                lines.append(f'{endpoint:<42}{times[0]:12.2f}{times[1]:12.2f}{times[2]:12.2f}')
        elif line.strip():
            # curl errors identify the failed phase and must not disappear.
            lines.append(RED + line + RESET)
    return '\n'.join(lines) if found else None


def display_output(result):
    kind = result.get('id')
    formatter = {'udp': iperf_summary, 'sustained': iperf_summary, 'dns': dns_summary, 'cdn': cdn_summary}.get(kind)
    summary = formatter(result) if formatter else None
    return summary if summary is not None else result.get('ansi_output') or result.get('output') or 'No output'
