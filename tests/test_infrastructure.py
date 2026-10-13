import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from cryptography.fernet import Fernet
from bot.services.infrastructure.service import InfrastructureService
from bot.services.infrastructure.settings import choose_sni, validate_address, validate_hostname
from bot.services.infrastructure.diagnostics import privacy_patch, clean_output
from bot.services.panel_infrastructure_api import PanelInfrastructureApi
from bot.services.infrastructure.reports import aggregate, render_reports
from bot.services.infrastructure.report_data import parse_result, result_measurements


class InfrastructureTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_diagnostic_checkpoint_renders_without_contacting_vps(self):
        import json
        from unittest.mock import patch
        from bot.services.infrastructure.diagnostics import run_diagnostics
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'reports' / '18'
            directory.mkdir(parents=True)
            directory.joinpath('results.json').write_text(json.dumps([
                {'id': 'cpu', 'round': i, 'status': 'ok', 'output': 'events per second: 10'}
                for i in (1, 2)]))
            service = SimpleNamespace(settings=SimpleNamespace(INFRA_ARTIFACT_DIR=root))
            with patch('bot.services.infrastructure.diagnostics.CATALOG', [('cpu',)]), \
                    patch('bot.services.infrastructure.diagnostics.EXTRA', []), \
                    patch('bot.services.infrastructure.diagnostics.scripts', new=AsyncMock()) as fetch, \
                    patch('bot.services.infrastructure.diagnostics.finalize_report', new=AsyncMock()) as finish:
                await run_diagnostics(service, SimpleNamespace(id=18, kind='large'),
                    SimpleNamespace(name='node'), SimpleNamespace(id=6), {'large_repeats': 2})
            fetch.assert_not_awaited()
            finish.assert_awaited_once()
            self.assertEqual(len(finish.await_args.args[-1]), 2)

    def test_iperf_json_summary_keeps_received_rate_and_loss(self):
        import json
        result = {'id': 'udp', 'output': json.dumps({'end': {
            'sum_sent': {'bits_per_second': 100000000},
            'sum_received': {'bits_per_second': 50000000, 'lost_percent': 1.25, 'jitter_ms': 0.02}}})}
        facts = dict(parse_result(result)['facts'])
        self.assertEqual(facts['Передано'], '100.00 Mbps')
        self.assertEqual(facts['Принято'], '50.00 Mbps')
        self.assertEqual(facts['Принято потери'], '1.25 %')
        self.assertEqual(result_measurements(result)['sum_received bits_per_second [Mbps]'], 50)

    def test_yabs_blank_geekbench_scores_are_partial_and_valid_scores_are_preserved(self):
        from bot.services.infrastructure.diagnostics import incomplete_checks
        self.assertTrue(incomplete_checks('yabs', 'Single Core | \nMulti Core | \n'))
        result = {'id': 'yabs', 'output': 'Single Core | 1200\nMulti Core | 2100\n'}
        self.assertEqual(incomplete_checks('yabs', result['output']), [])
        self.assertEqual(result_measurements(result)['Geekbench single'], 1200)

    async def test_fresh_config_does_not_share_mutable_key_defaults(self):
        from unittest.mock import patch
        from bot.services.infrastructure.settings import read_config
        with patch('bot.services.infrastructure.settings.app_settings_dal.get_setting', new=AsyncMock(return_value=None)):
            first = await read_config(object())
            first['ssh_keys'].append({'name': 'uncommitted', 'key': 'test'})
            second = await read_config(object())
        self.assertEqual(second['ssh_keys'], [])
        self.assertEqual(second['large_repeats'], 1)

    def test_single_squad_expiry_restores_only_the_changed_disabled_field(self):
        before, after = PanelInfrastructureApi.exclusion_values({
            'internalSquads': {'mode': 'ALLOW_ONLY', 'squads': ['tg']}, 'isDisabled': False}, 'tg', 3)
        self.assertEqual(before, {'isDisabled': False})
        self.assertEqual(after, {'isDisabled': True})

    async def test_billing_binding_refresh_does_not_follow_reassigned_invoice(self):
        service = InfrastructureService(SimpleNamespace(), SimpleNamespace(identities=SimpleNamespace(scope='test')), None, None)
        node = SimpleNamespace(panel_uuid='node', billing_uuid='obsolete', provider_uuid='old-provider')
        entry = await service.billing_entry(None, node, {'billingNodes': [
            {'uuid': 'obsolete', 'nodeUuid': 'other-node', 'providerUuid': 'wrong-provider'},
            {'uuid': 'current', 'node': {'uuid': 'node'}, 'provider': {'uuid': 'provider'}}]})
        self.assertEqual(entry['uuid'], 'current')
        self.assertEqual(node.billing_uuid, 'current')
        self.assertEqual(node.provider_uuid, 'provider')


    def test_report_parser_preserves_network_directions_and_normalizes_units(self):
        result = {'id': 'yabs', 'output': 'Provider | Location | Send | Recv | Ping\n'
                  'Cloudvider | London, UK | 500 Mbits/sec | 1.25 Gbits/sec | 3 ms'}
        data = parse_result(result)
        self.assertEqual(data['bars'][0]['value'], 1250)
        metrics = result_measurements(result)
        self.assertEqual(metrics['Cloudvider · London, UK download [Mbps]'], 1250)
        self.assertEqual(metrics['Cloudvider · London, UK upload [Mbps]'], 500)
        result = {'id': 'tlab_speed', 'output': 'Moscow     500 Mbit/s     900 Mbit/s     5 ms'}
        self.assertEqual(parse_result(result)['bars'][0]['value'], 900)
        result = {'id': 'yabs', 'output': 'iperf3 Network Speed Tests (IPv4):\n'
                  'Host | London | 1 Mbits/sec | 2 Mbits/sec\niperf3 Network Speed Tests (IPv6):\n'
                  'Host | London | 3 Mbits/sec | 4 Mbits/sec'}
        self.assertEqual(len(result_measurements(result)), 4)

    def test_report_keeps_disk_units_separate_from_network_and_parses_latency(self):
        result = {'id': 'yabs', 'output': 'Block Size | 4k (IOPS) | 1m (IOPS)\n'
                  'Read | 40.90 MB/s (9.9k) | 1.36 GB/s (1.3k)\n'
                  'Write | 41.01 MB/s (10.0k) | 1.45 GB/s (1.3k)\n'
                  'iperf3 Network Speed Tests (IPv4):\n'
                  'Clouvider | London | 53.1 Mbits/sec | 52.3 Mbits/sec'}
        data = parse_result(result)
        self.assertEqual(len(data['bars']), 1)
        self.assertEqual(data['bars'][0]['value'], 52.3)
        self.assertEqual(result_measurements(result)['fio 1m read [MB/s]'], 1360)
        cdn = {'id': 'cdn', 'output': 'https://example.com connect=0.01 tls=0.02 ttfb=0.03\n'
               'https://unreachable.example connect=0 tls=0 ttfb=0'}
        self.assertEqual(result_measurements(cdn)['https://example.com ttfb [ms]'], 30)
        self.assertFalse(any('unreachable' in key for key in result_measurements(cdn)))
        dns = {'id': 'dns', 'output': '1.1.1.1\n;; Query time: 0 msec\n8.8.8.8\n;; Query time: 20 msec'}
        self.assertEqual(result_measurements(dns), {'1.1.1.1 query [ms]': 0, '8.8.8.8 query [ms]': 20})

    def test_report_parser_excludes_region_legend_and_preserves_failed_service(self):
        result = {'id': 'ipregion', 'output': 'Service    IPv4\nNetflix    US\nReddit    Blocked\n'
                  'Legend\nUS    United States    100%'}
        data = parse_result(result)
        self.assertEqual(len(data['services']), 2)
        self.assertEqual(data['services'][1]['status'], 'bad')
        result = {'id': 'ipcheck', 'output': 'Service:  Netflix  Reddit\nStatus:   NF.Only  Blocked\nRegion:   US  -'}
        self.assertEqual([s['status'] for s in parse_result(result)['services']], ['warn', 'bad'])

    def test_terminal_progress_does_not_expand_report(self):
        self.assertEqual(clean_output('Checking 10%\rChecking 90%\rDone\nCPU: 100'), 'Done\nCPU: 100')

    def test_diagnostic_checkpoint_keeps_completed_steps_and_rejects_another_vps(self):
        import json
        from bot.services.infrastructure.diagnostics import resume_results
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            results = [{'id': 'ipregion', 'round': 1, 'status': 'ok', 'output': 'FR'}]
            (path / 'results.json').write_text(json.dumps(results))
            self.assertEqual(resume_results(path, 1, 'medium'), results)
            (path / 'results.json').write_text(json.dumps({'deployment_id': 2, 'level': 'medium', 'results': results}))
            with self.assertRaises(ValueError):
                resume_results(path, 1, 'medium')

    def test_credentials_cipher_does_not_store_password(self):
        key = Fernet.generate_key().decode()
        service = InfrastructureService(SimpleNamespace(INFRA_ENCRYPTION_KEY=key),
            SimpleNamespace(identities=SimpleNamespace(scope='test')), None, None)
        encrypted = service.cipher().encrypt(b'admin-password').decode()
        self.assertNotIn('admin-password', encrypted)
        self.assertEqual(service.cipher().decrypt(encrypted.encode()), b'admin-password')

    def test_sni_balances_and_never_uses_unknown_pool_entry(self):
        deployments = [SimpleNamespace(sni='a.example') for _ in range(3)]
        self.assertEqual(choose_sni(['a.example', 'b.example'], deployments), 'b.example')
        self.assertIsNone(choose_sni([], deployments))

    def test_address_rejects_shell_payload(self):
        for value in ['example.com; touch /tmp/pwn', '$(id)', '-option', 'a/b']:
            with self.assertRaises(ValueError):
                validate_address(value)
        self.assertEqual(validate_address('2001:db8::1'), '2001:db8::1')

    def test_privacy_patch_blocks_report_upload_but_preserves_geekbench_exception(self):
        source = 'echo results\nreport_link=$(curl -X POST https://upload.check.place -d secret)\nshow_ad\n'
        patched = privacy_patch('ipcheck', source)
        self.assertNotIn('upload.check.place', patched)
        self.assertNotIn('show_ad', patched)
        yabs = 'geekbench --upload\ncurl -X POST --data "$JSON_RESULT" "$JSON_SITE"\n'
        patched = privacy_patch('yabs', yabs)
        self.assertIn('geekbench --upload', patched)
        self.assertNotIn('curl -X POST', patched)

    async def test_quick_check_waits_for_panel_delivered_listener(self):
        from unittest.mock import patch, Mock
        from bot.services.infrastructure.diagnostics import quick_check
        connection = SimpleNamespace(run=AsyncMock(side_effect=[
            SimpleNamespace(stdout='true', exit_status=0), SimpleNamespace(stdout=':2222'),
            SimpleNamespace(stdout='true', exit_status=0), SimpleNamespace(stdout=':2222 :443')]))
        context = AsyncMock()
        context.__aenter__.return_value = connection
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        service = SimpleNamespace(cipher=lambda: SimpleNamespace(decrypt=lambda raw: b'password'),
                                  settings=None, api=SimpleNamespace(call=AsyncMock(return_value={
                                      'isConnected': True, 'address': '203.0.113.1'})))
        deployment = SimpleNamespace(credentials='encrypted', address='203.0.113.1', host_address='203.0.113.1')
        with patch('bot.services.infrastructure.diagnostics.asyncssh.connect', return_value=context), \
             patch('bot.services.infrastructure.diagnostics.ssh_arguments', return_value={}), \
             patch('bot.services.infrastructure.diagnostics.asyncio.sleep', new=AsyncMock()) as sleep, \
             patch('bot.services.infrastructure.diagnostics.asyncio.open_connection', new=AsyncMock(return_value=(None, writer))):
            result = await quick_check(service, deployment, SimpleNamespace(panel_uuid='node'))
        self.assertIn('TCP 443: OK', result)
        sleep.assert_awaited_once()
        writer.close.assert_called_once()

    async def test_quick_check_rejects_connection_to_another_deployment(self):
        from unittest.mock import patch
        from bot.services.infrastructure.diagnostics import quick_check
        connection = SimpleNamespace(run=AsyncMock(side_effect=[
            SimpleNamespace(stdout='true', exit_status=0), SimpleNamespace(stdout=':2222 :443')]))
        context = AsyncMock()
        context.__aenter__.return_value = connection
        service = SimpleNamespace(cipher=lambda: SimpleNamespace(decrypt=lambda raw: b'password'), settings=None,
                                  api=SimpleNamespace(call=AsyncMock(return_value={
                                      'isConnected': True, 'address': '203.0.113.1'})))
        deployment = SimpleNamespace(credentials='encrypted', address='203.0.113.2', host_address='203.0.113.2')
        with patch('bot.services.infrastructure.diagnostics.asyncssh.connect', return_value=context), \
             patch('bot.services.infrastructure.diagnostics.ssh_arguments', return_value={}), \
             patch('bot.services.infrastructure.diagnostics.time.monotonic', side_effect=[0, 95]), \
             patch('bot.services.infrastructure.diagnostics.asyncio.open_connection', new=AsyncMock()) as external:
            with self.assertRaisesRegex(RuntimeError, 'Адрес ноды'):
                await quick_check(service, deployment, SimpleNamespace(panel_uuid='node'))
        external.assert_not_awaited()

    async def test_node_secret_supports_both_panel_versions(self):
        for field in ('pubKey', 'secretKey'):
            panel = SimpleNamespace(_request=AsyncMock(return_value={'response': {field: 'test-secret'}}))
            self.assertEqual(await PanelInfrastructureApi(panel).node_secret(), 'test-secret')

    async def test_provision_retry_cannot_adopt_another_node_with_same_name(self):
        from unittest.mock import patch
        from bot.services.infrastructure.provisioning import provision
        node = SimpleNamespace(id=1, panel_uuid=None, name='node1 (XX)', country='XX', provider_uuid='provider')
        deployment = SimpleNamespace(address='203.0.113.1', profile_uuid='profile', inbound_uuid='inbound', sni=None)
        api = SimpleNamespace(node_secret=AsyncMock(return_value='secret'), call=AsyncMock(), nodes=AsyncMock(return_value=[{
            'name': node.name, 'address': '203.0.113.2', 'countryCode': 'XX',
            'configProfile': {'activeConfigProfileUuid': 'profile', 'activeInbounds': ['inbound']}}]))
        service = SimpleNamespace(api=api, panel=SimpleNamespace(get_inbounds_by_profile_uuid=AsyncMock(
            return_value=[{'uuid': 'inbound', 'port': 443, 'type': 'vless'}]),
            api_major=AsyncMock(return_value=2)))
        with patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()), \
             patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock()):
            with self.assertRaises(ValueError):
                await provision(service, node, deployment, {}, 1)
        self.assertIsNone(node.panel_uuid)
        api.call.assert_not_awaited()

    async def test_node_restart_sends_required_boolean_for_both_panel_versions(self):
        for major in (2, 3):
            for force in (False, True):
                with self.subTest(major=major, force=force):
                    panel = SimpleNamespace(api_major=AsyncMock(return_value=major), _request=AsyncMock(return_value={'response': {}}))
                    api = PanelInfrastructureApi(panel)
                    if force:
                        await api.restart_node('node-uuid', force=True)
                    else:
                        await api.restart_node('node-uuid')
                    request = panel._request.call_args
                    self.assertEqual(request.args, ('POST', '/nodes/node-uuid/actions/restart'))
                    self.assertEqual(request.kwargs['json'], {'forceRestart': force})

    async def test_reality_sni_requires_exact_allowed_name_before_installation(self):
        from unittest.mock import patch
        from bot.services.infrastructure.provisioning import provision
        deployment = SimpleNamespace(profile_uuid='profile', inbound_uuid='inbound', sni='vpn.example.com')
        service = SimpleNamespace(api=SimpleNamespace(), panel=SimpleNamespace(get_inbounds_by_profile_uuid=AsyncMock(
            return_value=[{'uuid': 'inbound', 'port': 443, 'type': 'vless', 'rawInbound': {
                'streamSettings': {'realitySettings': {'serverNames': ['*.example.com']}}}}])))
        with patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock()) as install:
            with self.assertRaises(ValueError):
                await provision(service, SimpleNamespace(), deployment, {}, 1)
        install.assert_not_awaited()

    async def test_replacement_restores_host_even_if_node_restart_rollback_fails(self):
        from unittest.mock import patch
        from bot.services.infrastructure.provisioning import provision
        node = SimpleNamespace(id=1, panel_uuid='node', host_uuid='host', country='XX', provider_uuid='provider')
        deployment = SimpleNamespace(address='203.0.113.2', host_address='203.0.113.2',
                                     profile_uuid='profile', inbound_uuid='inbound', sni=None)
        old = {'uuid': 'node', 'address': '203.0.113.1', 'port': 2222,
               'configProfile': {'activeConfigProfileUuid': 'profile', 'activeInbounds': ['inbound']}}
        host = {'uuid': 'host', 'address': '203.0.113.1', 'port': 443, 'nodes': ['node']}
        api = SimpleNamespace(node_secret=AsyncMock(return_value='secret'), nodes=AsyncMock(return_value=[old]),
                              patch_node=AsyncMock(), call=AsyncMock(),
                              restart_node=AsyncMock(side_effect=[None, RuntimeError('Rollback restart failed')]))
        service = SimpleNamespace(api=api, panel=SimpleNamespace(
            get_inbounds_by_profile_uuid=AsyncMock(return_value=[{'uuid': 'inbound', 'type': 'vless', 'port': 443}]),
            get_all_hosts=AsyncMock(return_value=[host])))
        with patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()), \
             patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock()), \
             patch('bot.services.infrastructure.diagnostics.quick_check', new=AsyncMock(
                 side_effect=[None, RuntimeError('Switch check failed')])):
            with self.assertRaisesRegex(RuntimeError, 'Rollback restart failed'):
                await provision(service, node, deployment, {}, 1)
        restored = api.call.call_args
        self.assertEqual(restored.args[:2], ('PATCH', '/hosts'))
        self.assertEqual(restored.args[2]['address'], host['address'])
        self.assertEqual(api.patch_node.call_args.args[1]['address'], old['address'])

    async def test_host_exclusion_only_changes_requested_squad(self):
        panel = SimpleNamespace(api_major=AsyncMock(return_value=3), _request=AsyncMock(return_value={'response': {}}))
        api = PanelInfrastructureApi(panel)
        host = {'uuid': 'host', 'internalSquads': {'mode': 'EXCLUDE', 'squads': ['other']}}
        previous, result = await api.host_access(host, 'tg', True)
        sent = panel._request.call_args.kwargs['json']
        self.assertEqual(sent['internalSquads']['squads'], ['other', 'tg'])
        self.assertEqual(host['internalSquads']['squads'], ['other'])
        self.assertEqual(previous['internalSquads']['squads'], ['other'])

    def test_expiry_restrictions_can_be_restored_after_panel_upgrade(self):
        legacy = {'excludedInternalSquads': ['other', 'tg']}
        translated = PanelInfrastructureApi.access_for_version(legacy, 3)
        self.assertEqual(translated, {'internalSquads': {'mode': 'EXCLUDE', 'squads': ['other', 'tg']}})
        self.assertEqual(legacy, {'excludedInternalSquads': ['other', 'tg']})
        self.assertEqual(PanelInfrastructureApi.access_for_version(translated, 2), legacy)

    def test_diagnostics_expose_skipped_subtests(self):
        from bot.services.infrastructure.diagnostics import incomplete_checks
        self.assertEqual(len(incomplete_checks('yabs', 'Less than 2GB available. Skipping disk test...')), 1)
        self.assertFalse(incomplete_checks('ipcheck', 'Reddit Blocked'))

    async def test_allow_only_removes_tg_without_exposing_host(self):
        panel = SimpleNamespace(api_major=AsyncMock(return_value=3), _request=AsyncMock(return_value={'response': {}}))
        api = PanelInfrastructureApi(panel)
        host = {'uuid': 'host', 'internalSquads': {'mode': 'ALLOW_ONLY', 'squads': ['tg', 'other']}}
        await api.host_access(host, 'tg', True)
        self.assertEqual(panel._request.call_args.kwargs['json']['internalSquads']['squads'], ['other'])
        host['internalSquads']['squads'] = ['tg']
        await api.host_access(host, 'tg', True)
        self.assertTrue(panel._request.call_args.kwargs['json']['isDisabled'])

    async def test_panel_edits_refresh_metadata_without_moving_ssh_credentials(self):
        service = InfrastructureService(SimpleNamespace(), SimpleNamespace(identities=SimpleNamespace(scope='test')), None, None)
        node = SimpleNamespace(id=1, panel_uuid='node', host_uuid='old-host', name='old', country='RU', provider_uuid='old-provider', billing_uuid='invoice')
        deployment = SimpleNamespace(address='203.0.113.1', host_address='old.example', credentials='encrypted',
            host_public_key='pinned', sni='old.sni', profile_uuid='old-profile', inbound_uuid='old-inbound', node_version='2.8.0')
        service.api.nodes = AsyncMock(return_value=[{'uuid': 'node', 'name': 'renamed', 'countryCode': 'DE',
            'providerUuid': 'new-provider', 'address': '203.0.113.2', 'versions': {'node': '3.4.2'}}])
        service.panel.get_all_hosts = AsyncMock(return_value=[{'uuid': 'new-host', 'nodes': ['node'], 'address': 'new.example',
            'sni': 'new.sni', 'inbound': {'configProfileUuid': 'new-profile', 'configProfileInboundUuid': 'new-inbound'}}])
        service.api.billing = AsyncMock(return_value={'billingNodes': []})
        service.deployment = AsyncMock(return_value=deployment)
        await service.refresh_node(None, node)
        self.assertEqual((node.name, node.country, node.provider_uuid, node.host_uuid, node.billing_uuid),
            ('renamed', 'DE', 'new-provider', 'new-host', None))
        self.assertEqual((deployment.host_address, deployment.sni, deployment.profile_uuid, deployment.inbound_uuid),
            ('new.example', 'new.sni', 'new-profile', 'new-inbound'))
        self.assertEqual((deployment.address, deployment.credentials, deployment.host_public_key),
            ('203.0.113.1', 'encrypted', 'pinned'))
        with self.assertRaisesRegex(ValueError, 'IP ноды изменён'):
            await service.validate_deployment(None, node, deployment)

    async def test_ambiguous_panel_hosts_are_not_reassigned_to_another_host(self):
        service = InfrastructureService(SimpleNamespace(), SimpleNamespace(identities=SimpleNamespace(scope='test')), None, None)
        node = SimpleNamespace(id=1, panel_uuid='node', host_uuid='deleted-host', name='node', country='DE', provider_uuid='', billing_uuid=None)
        service.api.nodes = AsyncMock(return_value=[{'uuid': 'node', 'address': '203.0.113.1'}])
        service.panel.get_all_hosts = AsyncMock(return_value=[{'uuid': h, 'nodes': ['node']} for h in ('one', 'two')])
        service.api.billing = AsyncMock(return_value={'billingNodes': []})
        service.deployment = AsyncMock(return_value=None)
        await service.refresh_node(None, node)
        self.assertIsNone(node.host_uuid)

    def test_console_pdf_preserves_text_colors_failure_rows_and_overview_order(self):
        from unittest.mock import patch
        from reportlab.pdfgen.canvas import Canvas
        from bot.services.infrastructure.terminal_report import console_rows, render_console_pdf, PALETTE
        ansi = 'CPU Model : \x1b[34mEPYC\x1b[0m\nNode Name     Upload       Download\nKazan         100 Mbps     \x1b[31mDownload failed\x1b[0m\nOther         busy         N/A\nIP API        Rate-limit\nScript by Teddysun'
        rows = console_rows(ansi)
        visible = '\n'.join(''.join(t for t, _ in row) for row in rows)
        self.assertIn('Kazan         100 Mbps     Download failed', visible)
        self.assertIn('busy         N/A', visible)
        self.assertIn('Rate-limit', visible)
        self.assertNotIn('Teddysun', visible)
        self.assertEqual(next(style.foreground for row in rows for text, style in row if text == 'Download failed'), PALETTE[1])
        records = [dict(id='ipregion', title='Region', output='Google  DE', status='ok', round=1),
                   dict(id='bench', title='System overview', output=clean_output(ansi), ansi_output=ansi, status='partial', round=1)]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(Canvas, 'drawImage') as raster, \
                patch.object(Canvas, 'drawString', autospec=True) as draw:
            render_console_pdf(Path(directory), {'node': 'node1', 'address': '203.0.113.1',
                'created_at': '2026-10-13', 'results': records}, [])
            raster.assert_not_called()
            text = [call.args[-1] for call in draw.call_args_list]
            self.assertIn('Download failed', text)
            self.assertLess(next(i for i,v in enumerate(text) if 'System overview' in v), next(i for i,v in enumerate(text) if 'Region |' in v))
            self.assertTrue((Path(directory)/'report.pdf').read_bytes().startswith(b'%PDF'))

    def test_console_styles_keep_background_and_reset_without_losing_table_spaces(self):
        from bot.services.infrastructure.terminal_report import console_rows, PALETTE
        row = console_rows('Service   \x1b[42;1mYes\x1b[0m    \x1b[38;2;12;34;56mDE\x1b[0m')[0]
        self.assertEqual(''.join(t for t, _ in row), 'Service   Yes    DE')
        self.assertEqual(row[1][1].background, PALETTE[2])
        self.assertTrue(row[1][1].bold)
        self.assertIsNone(row[2][1].background)
        self.assertEqual(row[3][1].foreground, '#0c2238')

    def test_report_rendering_has_no_sponsor_and_keeps_rounds(self):
        results = [{'title': 'CPU', 'status': 'ok', 'round': i, 'output': 'Процессор работает',
                    'metrics': {'speed': value}} for i, value in enumerate([100, 200, 300], 1)]
        self.assertEqual(aggregate(results)[0]['median'], 200)
        with tempfile.TemporaryDirectory() as directory:
            files = render_reports(Path(directory), {'node': 'node1 (RU)', 'address': '203.0.113.1',
                'created_at': '2026-10-12T00:00:00+00:00', 'level': 'large', 'results': results})
            self.assertTrue((Path(directory) / 'report.pdf').exists())
            self.assertEqual(files, ['report.pdf'])
        self.assertEqual(clean_output('CPU: 100\npowered by sponsor\nhttps://t.me/somebody'), 'CPU: 100')
