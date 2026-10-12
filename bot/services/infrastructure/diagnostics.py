"""Fixed diagnostic catalog. Scripts are hash-verified and privacy-patched locally."""
import asyncio
import hashlib
import html
import json
import re
import shlex
import statistics
import time
from pathlib import Path
from datetime import datetime, timezone

import aiohttp
import asyncssh
from aiogram.types import FSInputFile, InputMediaPhoto
from db.models import InfrastructureReport as Report
from .provisioning import ssh_arguments, stage

CATALOG = [
    ('ipregion', 'IP Region', 'ipregion', '', 600),
    ('geo', 'Геоблокировки', 'censorcheck', '--mode geoblock', 600),
    ('dpi', 'DPI', 'censorcheck', '--mode dpi', 600),
    ('tlab_censor', 'Доступность сервисов', 'tlab_censor', '', 600),
    ('ru_speed', 'iPerf3 — Россия', 'ru_speed', '', 900),
    ('tlab_speed', 'iPerf3 — дополнительные направления РФ', 'tlab_speed', '', 1200),
    ('yabs', 'YABS — сервер и международная сеть', 'yabs', '-4', 1800),
    ('ipcheck', 'IP Check — сервисы', 'ipcheck', '-E -n -p', 900),
    ('bench', 'Система и скорость', 'bench', '', 1200),
    ('ipquality', 'Качество IP', 'ipcheck', '-E -n -p', 900),
    ('cpu', 'sysbench CPU', None, 'sysbench cpu --threads=2 --time=15 run', 60),
]
# Additional checks use packaged tools; no downloaded scripts/images or report uploads.
EXTRA = [
    ('udp', 'UDP — скорость и потери', None, 'iperf3 -c iperf.he.net -u -b 100M -t 15 -J', 45),
    ('sustained', 'Устойчивость скорости 90 секунд', None, 'iperf3 -c ping.online.net -t 90 -i 15 -J', 120),
    ('ipv6', 'IPv6', None, 'curl -6 -fsS --max-time 15 https://cloudflare.com/cdn-cgi/trace', 30),
    ('dns', 'DNS — сравнение резолверов', None, 'for resolver in 1.1.1.1 8.8.8.8 9.9.9.9; do echo "$resolver"; dig +time=3 +tries=1 @"$resolver" example.com A; done', 30),
    ('route', 'Маршрут до РФ', None, 'mtr --report --report-cycles 10 --no-dns spd-rudp.hostkey.ru', 45),
    ('cdn', 'CDN — соединение и TTFB', None, "for url in https://www.cloudflare.com https://www.google.com https://www.microsoft.com; do curl -sS -o /dev/null --max-time 15 -w '%{url_effective} connect=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer}\n' \"$url\"; done", 60),
    ('bufferbloat', 'Задержка под нагрузкой', None, 'ping -c 25 -i 0.5 1.1.1.1 & p=$!; iperf3 -c ping.online.net -t 10; wait "$p"', 40),
    ('memory', 'Память — производительность', None, 'sysbench memory --time=15 --memory-total-size=100G run', 45),
]


def privacy_patch(name, content):
    if name == 'ipcheck':
        # Disable report upload unconditionally, not merely through caller flags.
        content = '\n'.join(':' if 'https://upload.check.place' in line else line
                            for line in content.splitlines())
        content = re.sub(r'^show_ad\s*$', ':', content, flags=re.M)
        content = re.sub(r'^countRunTimes\s*$', ':', content, flags=re.M)
        # Interactive menu recursively fetches executable scripts; unreachable in
        # our invocation, but remove those calls as defence in depth.
        content = re.sub(r'^eval "bash <\(curl.*$', ':', content, flags=re.M)
    elif name == 'yabs':
        content = '\n'.join(':' if ('JSON_SITE' in line and ('curl ' in line or 'wget ' in line))
                            else line for line in content.splitlines())
        # Geekbench --upload is explicitly authorized by the owner.
    elif name == 'tlab_speed':
        content = re.sub(r'^countRunTimes\s*$', ':', content, flags=re.M)
    elif name == 'tlab_censor':
        # RIPE Atlas creates public measurements with this script's bundled key.
        # Do not create them; retain all direct service/network checks.
        start = content.index('if [[ -n "$CURRENT_IP" ]] && [[ -n "$RIPE_API_KEY" ]]; then')
        end = content.index('\necho "$LINE_SEP"\n\nend_time=', start)
        content = content[:start] + 'echo "Внешние RIPE Atlas измерения отключены"\n' + content[end:]
    return content + '\n'


async def scripts(directory):
    manifest = json.loads((Path(__file__).resolve().parents[3] / 'infra/diagnostics/scripts.json').read_text())
    paths = {}
    # Some command-line endpoints serve an HTML landing page to Python clients.
    # Request their curl representation, then verify the exact script hash.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60),
                                     headers={'User-Agent': 'curl/8.14.1'}) as client:
        for name, spec in manifest.items():
            path = directory / f'{name}.sh'
            # Always verify the original before applying the reviewed transforms.
            async with client.get(spec['url']) as response:
                response.raise_for_status()
                content = await response.read()
            if hashlib.sha256(content).hexdigest() != spec['sha256']:
                raise ValueError(f'Изменился скрипт {name}; требуется проверка новой версии')
            path.write_text(privacy_patch(name, content.decode()))
            path.chmod(0o600)
            paths[name] = path
    return paths


async def quick_check(service, deployment, node):
    password = service.cipher().decrypt(deployment.credentials.encode()).decode()
    deadline = time.monotonic() + 90
    error = 'Контейнер ноды не работает'
    async with asyncssh.connect(**ssh_arguments(deployment, service.settings, password)) as connection:
        # Compose returns before the management listener and panel-delivered
        # Xray configuration are ready. Allow startup without reinstalling.
        while True:
            result = await connection.run('docker inspect -f "{{.State.Running}}" remnanode', timeout=15)
            if result.exit_status or result.stdout.strip() != 'true':
                error = 'Контейнер ноды не работает'
            else:
                result = await connection.run("ss -ltn | grep -E '(:443|:2222)[[:space:]]'", timeout=15)
                if ':443' not in result.stdout or ':2222' not in result.stdout:
                    error = 'Нода не слушает порты 443 и 2222'
                else:
                    data = await service.api.call('GET', f'/nodes/{node.panel_uuid}')
                    if data.get('isConnected'):
                        try:
                            reader, writer = await asyncio.wait_for(asyncio.open_connection(deployment.host_address, 443), 15)
                            writer.close(); await writer.wait_closed()
                            return 'Контейнер, связь с панелью и TCP 443: OK. Проверка не является полноценным VPN-сеансом.'
                        except (OSError, TimeoutError):
                            error = 'TCP 443 недоступен извне'
                    else:
                        error = 'Панель не подключена к ноде'
            if time.monotonic() >= deadline:
                raise RuntimeError(error)
            await asyncio.sleep(2)


ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def clean_output(raw):
    # Terminal progress overwrites the current line with CR; retain its final
    # state rather than producing hundreds of duplicate progress rows.
    raw = '\n'.join(line.split('\r')[-1] for line in ANSI.sub('', raw).split('\n'))
    raw = re.sub(r'[\u2800-\u28ff]\x08?|\x08', '', raw)
    # Remove upstream promotion from user-facing artifacts.
    return '\n'.join(line for line in raw.splitlines() if not any(token in line.lower() for token in (
        'sponsor', 'powered by', 'follow:', 'follow ', 't.me/', 'tracerlab', 'stencloud', 'bedolaga',
        'github.com/', 'bench.sh | bash', 'yabs.sh | bash', 'forked by', 'made with', 'author:', 'script by',
        'thanks for running', 'from the community, for the community')))


def measurements(output):
    values = {}
    for line in output.splitlines():
        match = re.search(r'(.+?)\s+([0-9]+(?:\.[0-9]+)?)\s*(Mbps|Mbits/sec|MB/s|GB/s|ms|events per second)\b', line)
        if match:
            label, number, unit = match.groups()
            values[f'{label.strip()} [{unit}]'] = float(number)
    return values


def incomplete_checks(test_id, output):
    patterns = {
        'yabs': r'[^\n]*(?:Skipping disk test|Geekbench[^\n]*test failed|Skipping Geekbench)[^\n]*',
        'ipregion': r'[^\n]*(?:Missing dependencies|IP address is not available)[^\n]*',
        'geo': r'[^\n]*Missing dependencies[^\n]*',
        'dpi': r'[^\n]*Missing dependencies[^\n]*',
        'bench': r'[^\n]*(?:Failed to download speedtest|Failed to parse results|Test failed)[^\n]*',
    }
    return re.findall(patterns[test_id], output, re.I) if test_id in patterns else []


def resume_results(directory, deployment_id, level):
    path = directory / 'results.json'
    if not path.exists():
        return []
    try:
        saved = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(saved, dict):
        if saved.get('deployment_id') != deployment_id or saved.get('level') != level:
            raise ValueError('Сохранённые результаты относятся к другому VPS или уровню теста')
        saved = saved['results']
    return saved if isinstance(saved, list) else []


async def run_diagnostics(service, job, node, deployment, config):
    directory = Path(service.settings.INFRA_ARTIFACT_DIR) / 'reports' / str(job.id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = resume_results(directory, deployment.id, job.kind)
    from .report_data import result_measurements
    for result in results:
        result['output'] = clean_output(result['output'])
        result['warnings'] = incomplete_checks(result.get('id'), result['output'])
        if result['status'] == 'ok' and result['warnings']:
            result['status'] = 'partial'
        result['metrics'] = result_measurements(result) or measurements(result['output'])
    if job.kind == 'small':
        results.append({'title': 'Проверка ноды', 'status': 'ok', 'round': 1,
                        'output': await quick_check(service, deployment, node), 'metrics': {}})
    else:
        paths = await scripts(directory)
        password = service.cipher().decrypt(deployment.credentials.encode()).decode()
        remote = f'/tmp/rw-diagnostics-{job.id}'
        async with asyncssh.connect(**ssh_arguments(deployment, service.settings, password)) as connection:
            await stage(service, job.id, 'dependencies')
            await connection.run('DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
                'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl wget jq util-linux dnsutils '
                'iperf3 sysbench fio mtr-tiny netcat-openbsd bc iputils-ping openssl bsdextrautils', check=True, timeout=600)
            await connection.run(f'mkdir -m 700 {remote}', check=True)
            try:
                async with connection.start_sftp_client() as sftp:
                    for name, path in paths.items():
                        await sftp.put(str(path), f'{remote}/{name}.sh')
                catalog = CATALOG + (EXTRA if job.kind == 'large' else [])
                repeats = config['large_repeats'] if job.kind == 'large' else 1
                for iteration in range(1, repeats + 1):
                    for test_id, title, script, command, timeout in catalog:
                        if any(r.get('id') == test_id and r.get('round') == iteration for r in results):
                            continue
                        await stage(service, job.id, f'{iteration}/{repeats}: {test_id}')
                        actual = f'bash {remote}/{script}.sh {command}' if script else command
                        # Every process group is isolated and bounded. Cancellation
                        # kills the process group, not just the SSH channel.
                        await connection.run(f'rm -f {remote}/exit.txt', timeout=10)
                        script_command = f'cd {remote}; export LC_ALL=C.UTF-8 TERM=dumb NO_COLOR=1; timeout --kill-after=10 {timeout} bash -c {shlex.quote(actual)}; echo $? > {remote}/exit.txt'
                        wrapped = f'setsid bash -c {shlex.quote(script_command)} > {remote}/out.txt 2>&1 < /dev/null & echo $!'
                        started = await connection.run(wrapped, timeout=15)
                        pid = started.stdout.strip()
                        if not pid.isdigit():
                            raise RuntimeError('Не удалось запустить диагностический процесс')
                        began = time.monotonic()
                        try:
                            while True:
                                alive = await connection.run(f'test -f {remote}/exit.txt', timeout=10)
                                if alive.exit_status == 0:
                                    break
                                if time.monotonic() - began > timeout + 20:
                                    await connection.run(f'kill -- -{pid} 2>/dev/null || true', timeout=10)
                                    break
                                await asyncio.sleep(2)
                        except asyncio.CancelledError:
                            await connection.run(f'kill -- -{pid} 2>/dev/null || true', timeout=10)
                            raise
                        captured = await connection.run(f'head -c 2000000 {remote}/out.txt', timeout=30)
                        output = clean_output(captured.stdout)
                        exit_result = await connection.run(f'cat {remote}/exit.txt 2>/dev/null', timeout=10)
                        outcome = 'ok' if exit_result.stdout.strip() == '0' else ('timeout' if exit_result.stdout.strip() == '124' else 'failed')
                        result = {'id': test_id, 'title': title, 'round': iteration,
                                  'status': outcome,
                                  'seconds': round(time.monotonic() - began, 1),
                                  'output': output, 'metrics': measurements(output)}
                        result['warnings'] = incomplete_checks(test_id, output)
                        if outcome == 'ok' and result['warnings']:
                            result['status'] = 'partial'
                        result['metrics'] = result_measurements(result) or result['metrics']
                        results.append(result)
                        (directory / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
            finally:
                await connection.run(f'rm -rf -- {remote}', timeout=30)
    manifest = {'node': node.name, 'address': deployment.address, 'level': job.kind,
                'deployment_id': deployment.id, 'created_at': datetime.now(timezone.utc).isoformat(),
                'results': results, 'catalog_version': 1}
    (directory / 'results.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    from .reports import render_reports
    artifacts = await asyncio.to_thread(render_reports, directory, manifest)
    async with service.factory() as session:
        session.add(Report(job_id=job.id, deployment_id=deployment.id, level=job.kind,
                           manifest=json.dumps({'directory': str(job.id), 'files': artifacts})))
        await session.commit()
    await send_artifacts(service, job.requested_by, directory, artifacts, job.kind)


async def send_artifacts(service, admin_id, directory, artifacts, level):
    if level == 'large':
        await service.bot.send_document(admin_id, FSInputFile(directory / artifacts[-1]))
    elif level == 'medium':
        images = [name for name in artifacts if name.endswith('.png')]
        for index in range(0, len(images), 10):
            group = images[index:index + 10]
            if len(group) == 1:
                await service.bot.send_photo(admin_id, FSInputFile(directory / group[0]))
            else:
                await service.bot.send_media_group(admin_id, [InputMediaPhoto(media=FSInputFile(directory / name)) for name in group])
    else:
        await service.bot.send_document(admin_id, FSInputFile(directory / 'results.json'))
