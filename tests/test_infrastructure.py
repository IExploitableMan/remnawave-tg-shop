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
                                  settings=None, api=SimpleNamespace(call=AsyncMock(return_value={'isConnected': True})))
        deployment = SimpleNamespace(credentials='encrypted', host_address='203.0.113.1')
        with patch('bot.services.infrastructure.diagnostics.asyncssh.connect', return_value=context), \
             patch('bot.services.infrastructure.diagnostics.ssh_arguments', return_value={}), \
             patch('bot.services.infrastructure.diagnostics.asyncio.sleep', new=AsyncMock()) as sleep, \
             patch('bot.services.infrastructure.diagnostics.asyncio.open_connection', new=AsyncMock(return_value=(None, writer))):
            result = await quick_check(service, deployment, SimpleNamespace(panel_uuid='node'))
        self.assertIn('TCP 443: OK', result)
        sleep.assert_awaited_once()
        writer.close.assert_called_once()

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
            return_value=[{'uuid': 'inbound', 'port': 443, 'type': 'vless'}])))
        with patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()), \
             patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock()):
            with self.assertRaises(ValueError):
                await provision(service, node, deployment, {}, 1)
        self.assertIsNone(node.panel_uuid)
        api.call.assert_not_awaited()

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

    def test_report_rendering_has_no_sponsor_and_keeps_rounds(self):
        results = [{'title': 'CPU', 'status': 'ok', 'round': i, 'output': 'Процессор работает',
                    'metrics': {'speed': value}} for i, value in enumerate([100, 200, 300], 1)]
        self.assertEqual(aggregate(results)[0]['median'], 200)
        with tempfile.TemporaryDirectory() as directory:
            files = render_reports(Path(directory), {'node': 'node1 (RU)', 'address': '203.0.113.1',
                'created_at': '2026-10-12T00:00:00+00:00', 'level': 'large', 'results': results})
            self.assertTrue((Path(directory) / 'report.pdf').exists())
            self.assertEqual(len([f for f in files if f.endswith('.png')]), 3)
        self.assertEqual(clean_output('CPU: 100\npowered by sponsor\nhttps://t.me/somebody'), 'CPU: 100')
