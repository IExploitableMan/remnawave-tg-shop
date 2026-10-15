"""Bridge (haproxy SNI fronting) creation, compensation and handler flow."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from bot.handlers.admin.infrastructure import InfrastructureStates, infrastructure_callback, infrastructure_input
from bot.middlewares.i18n import JsonI18n
from bot.services.infrastructure.bridge import add_profile_sni, create_bridge, create_bridge_host, remove_profile_sni
from bot.services.infrastructure.provisioning import ansible_bridge


class FakeBridgePanel:
    def __init__(self, wildcard=True):
        inbounds = [{'tag': 'VLESS-443', 'port': 443, 'streamSettings': {'realitySettings': {
                        'serverNames': ['www.google.com'], 'dest': 'www.google.com:443'}}}]
        if wildcard:
            inbounds.append({'tag': 'VLESS-WILDCARD', 'port': 443, 'streamSettings': {'realitySettings': {
                        'serverNames': ['cdn.google.com'], 'dest': 'www.google.com:443'}}})
        self.profile = {'uuid': 'profile', 'name': 'Common',
            'config': {'inbounds': inbounds, 'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}]},
            'inbounds': ([{'uuid': 'in-443', 'tag': 'VLESS-443'},
                          {'uuid': 'in-wildcard', 'tag': 'VLESS-WILDCARD'}] if wildcard
                         else [{'uuid': 'in-443', 'tag': 'VLESS-443'}])}
        self.restart_node = AsyncMock()
        self.node = {'uuid': 'node', 'isConnected': True, 'isConnecting': False, 'hostUuid': 'direct', 'configProfile': {
            'activeConfigProfileUuid': 'profile', 'activeInbounds': ['in-443']}}
        self.hosts = [{'uuid': 'direct', 'address': '203.0.113.1', 'port': 443, 'nodes': ['node'],
                       'remark': 'node1 (DE)',
                       'inbound': {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-443'}}]
        self.order = []
        self.fail_host_post = False
        self.strip_host_sni = False
        self.get_hosts_calls = 0

    async def nodes(self):
        return [copy.deepcopy(self.node)]

    async def call(self, method, path, payload=None):
        self.order.append(f'{method} {path}')
        if path == '/config-profiles/profile' and method == 'GET':
            return copy.deepcopy(self.profile)
        if path == '/config-profiles' and method == 'PATCH':
            previous = {entry['tag']: entry['uuid'] for entry in self.profile['inbounds']}
            self.profile['config'] = copy.deepcopy(payload['config'])
            self.profile['inbounds'] = [{'tag': entry['tag'], 'uuid': previous.get(entry['tag'], 'in-' + entry['tag'])} for entry in payload['config']['inbounds']]
            return {}
        if path == '/internal-squads' and method == 'GET':
            return {'internalSquads': []}
        if path == '/hosts' and method == 'GET':
            self.get_hosts_calls += 1
            hosts = copy.deepcopy(self.hosts)
            # Verification reads hosts last; earlier reads must see real data.
            if self.strip_host_sni and self.get_hosts_calls >= 7:
                for host in hosts:
                    host.pop('sni', None)
            return hosts
        if path == '/hosts' and method == 'POST':
            if self.fail_host_post:
                raise RuntimeError('injected host failure')
            host = dict(payload, uuid='bridge-host')
            self.hosts.append(copy.deepcopy(host))
            return copy.deepcopy(host)
        if path == '/hosts' and method == 'PATCH':
            target = next(host for host in self.hosts if host['uuid'] == payload['uuid'])
            target.update(copy.deepcopy(payload))
            return copy.deepcopy(target)
        if path.startswith('/hosts/') and method == 'DELETE':
            self.hosts = [host for host in self.hosts if host['uuid'] != path.split('/')[2]]
            return {}
        if path == '/nodes/bulk-actions/profile-modification':
            self.node['configProfile'] = copy.deepcopy(payload['configProfile'])
            return {}
        raise AssertionError((method, path))


BRIDGE_CONFIG = {'bridge_address': 'bridge.example.net', 'bridge_ssh_port': 22,
                 'bridge_ssh_user': 'root', 'bridge_host_public_key': 'ssh-ed25519 BRIDGEKEY'}


class BridgeCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeBridgePanel()
        self.deployment = SimpleNamespace(id=5, address='203.0.113.1',
            profile_uuid='profile', inbound_uuid='in-443')
        self.saved = SimpleNamespace(profile_uuid=None, inbound_uuid=None)
        self.service = SimpleNamespace(api=self.api, factory=self.fake_factory, scope='bridge-test')
        self.persisted = {}
        async def save(session, key, value, **kwargs): self.persisted[key] = value
        for patcher in [patch('db.dal.app_settings_dal.upsert_setting', new=AsyncMock(side_effect=save)),
                patch('bot.services.infrastructure.bridge.verify_connected', new=AsyncMock())]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ansible_calls = []

    def fake_factory(self):
        session = SimpleNamespace(
            get=AsyncMock(return_value=self.saved), commit=AsyncMock())
        return _SessionContext(session, self.saved)

    async def run_bridge(self, sni='dl.google.com'):
        self.ansible_calls = []
        async def fake_ansible(service, config, sni, backend, state='present'):
            self.ansible_calls.append((sni, backend, state))
            self.api.order.append(f'ansible {state}')
        with patch('bot.services.infrastructure.provisioning.ansible_bridge', new=fake_ansible), \
             patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()) as staged:
            await create_bridge(self.service, SimpleNamespace(id=1, panel_uuid='node', name='node1 (DE)', host_uuid='direct'),
                self.deployment, dict(BRIDGE_CONFIG), 1, sni)
        return staged

    def server_names(self, tag='VLESS-WILDCARD'):
        inbound = next(entry for entry in self.api.profile['config']['inbounds'] if entry['tag'] == tag)
        return inbound['streamSettings']['realitySettings']['serverNames']

    def direct_host(self):
        return next(host for host in self.api.hosts if host['uuid'] == 'direct')

    def bridge_host(self):
        return next(host for host in self.api.hosts if host['uuid'] == 'bridge-host')

    async def test_switches_node_and_host_to_wildcard_in_order(self):
        await self.run_bridge()
        # The wildcard profile accepted the SNI, then the node switched, then haproxy, then the host.
        self.assertLess(self.api.order.index('PATCH /config-profiles'),
                        self.api.order.index('POST /nodes/bulk-actions/profile-modification'))
        self.assertLess(self.api.order.index('POST /nodes/bulk-actions/profile-modification'),
                        self.api.order.index('ansible present'))
        self.assertLess(self.api.order.index('ansible present'),
                        self.api.order.index('POST /hosts'))
        self.assertEqual(self.server_names(), ['cdn.google.com', 'dl.google.com', 'www.google.com'])
        self.assertEqual(self.server_names('VLESS-443'), ['www.google.com'])
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-wildcard'])
        self.assertEqual(self.direct_host()['inbound'],
            {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-wildcard'})
        bridge = self.bridge_host()
        self.assertEqual(bridge['address'], 'bridge.example.net')
        self.assertEqual(bridge['sni'], 'dl.google.com')
        self.assertEqual(bridge['nodes'], ['node'])
        self.assertEqual(bridge['inbound'], {'configProfileUuid': 'profile',
                                             'configProfileInboundUuid': 'in-wildcard'})
        self.assertEqual(bridge['remark'], 'node1 (DE) bridge')
        # The node card follows the wildcard inbound the node now serves.
        self.assertEqual((self.saved.profile_uuid, self.saved.inbound_uuid), ('profile', 'in-wildcard'))

    async def test_bridge_keeps_unrelated_active_inbound_and_host(self):
        self.api.profile['config']['inbounds'].append({'tag': 'OTHER', 'port': 8443})
        self.api.profile['inbounds'].append({'tag': 'OTHER', 'uuid': 'other-inbound'})
        self.api.node['configProfile']['activeInbounds'].append('other-inbound')
        other = {'uuid': 'other-host', 'nodes': ['node'], 'address': '203.0.113.1', 'port': 8443,
            'inbound': {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'other-inbound'}}
        self.api.hosts.append(copy.deepcopy(other))
        await self.run_bridge()
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-wildcard', 'other-inbound'])
        self.assertEqual(next(h for h in self.api.hosts if h['uuid'] == 'other-host'), other)

    async def test_bridge_preserves_gemini_using_a_wildcard_sibling(self):
        from bot.services.infrastructure.gemini import prepare_gemini_config, has_gemini_routes
        config, tag = prepare_gemini_config(self.api.profile['config'], 'VLESS-443')
        self.api.profile['config'] = config
        self.api.profile['inbounds'].append({'tag': tag, 'uuid': 'source-gemini'})
        self.api.node['configProfile']['activeInbounds'] = ['source-gemini']
        self.api.hosts[0]['inbound']['configProfileInboundUuid'] = 'source-gemini'
        await self.run_bridge()
        wildcard_tag = 'VLESS-WILDCARD-GEMINI'
        target = next(i['uuid'] for i in self.api.profile['inbounds'] if i['tag'] == wildcard_tag)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], [target])
        self.assertTrue(has_gemini_routes(self.api.profile['config'], wildcard_tag))
        self.assertEqual(self.direct_host()['inbound'], self.bridge_host()['inbound'])
        self.assertEqual(json.loads(self.persisted['infra.bridge.bridge-test.1'])['direct_host_uuid'], 'direct')
        for host in ['direct', 'bridge-host']:
            saved = json.loads(self.persisted[f'infra.gemini.bridge-test.1.{host}'])
            self.assertEqual(saved['gemini_inbound_uuid'], target)
            self.assertEqual(saved['base_inbound_uuid'], 'in-wildcard')

    async def test_verification_failure_keeps_preexisting_bridge_intact(self):
        # A bridge host with the same SNI already exists (retry after a lost
        # compensation); a failed verification must not demolish it.
        self.api.hosts.append({'uuid': 'bridge-host', 'address': 'bridge.example.net',
            'sni': 'dl.google.com', 'nodes': ['node'], 'remark': 'node1 (DE) bridge',
            'inbound': {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-wildcard'}})
        self.api.node['configProfile']['activeInbounds'] = ['in-wildcard']
        self.api.hosts[0]['inbound'] = {'configProfileUuid': 'profile',
                                        'configProfileInboundUuid': 'in-wildcard'}
        self.api.strip_host_sni = True
        with self.assertRaisesRegex(RuntimeError, 'не подтвердила'):
            await self.run_bridge()
        self.assertIn('bridge-host', [host['uuid'] for host in self.api.hosts])
        self.assertNotIn('DELETE /hosts/bridge-host', self.api.order)
        self.assertNotIn(('dl.google.com', '203.0.113.1:443', 'absent'), self.ansible_calls)
        # The wildcard SNI list was not ours to clear either.
        self.assertEqual(self.server_names(), ['cdn.google.com'])

    async def test_remove_profile_sni_drops_only_that_sni(self):
        await add_profile_sni(self.api, 'profile', 'dl.google.com')
        changed = await remove_profile_sni(self.api, 'profile', 'dl.google.com')
        self.assertTrue(changed)
        self.assertEqual(self.server_names(), ['cdn.google.com'])
        self.assertFalse(await remove_profile_sni(self.api, 'profile', 'dl.google.com'))

    async def test_node_already_on_wildcard_skips_switching(self):
        self.api.node['configProfile']['activeInbounds'] = ['in-wildcard']
        self.api.hosts[0]['inbound'] = {'configProfileUuid': 'profile',
                                        'configProfileInboundUuid': 'in-wildcard'}
        await self.run_bridge()
        self.assertNotIn('POST /nodes/bulk-actions/profile-modification', self.api.order)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-wildcard'])

    async def test_existing_sni_in_wildcard_skips_patch(self):
        await add_profile_sni(self.api, 'profile', 'cdn.google.com')
        self.assertEqual(self.api.order, ['GET /config-profiles/profile'])

    async def test_profile_without_wildcard_creates_wildcard(self):
        self.api = FakeBridgePanel(wildcard=False)
        self.service.api = self.api
        await self.run_bridge()
        self.assertIn('VLESS-WILDCARD', [ib['tag'] for ib in self.api.profile['inbounds']])
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-VLESS-WILDCARD'])

    async def test_second_bridge_for_node_is_refused(self):
        self.api.hosts.append({'uuid': 'other-bridge', 'address': 'bridge.example.net',
            'sni': 'fonts.google.com', 'nodes': ['node'], 'remark': 'node1 (DE) bridge'})
        with self.assertRaisesRegex(ValueError, 'уже есть мост'):
            await create_bridge(self.service, SimpleNamespace(id=1, panel_uuid='node', name='node1 (DE)', host_uuid='direct'),
                self.deployment, dict(BRIDGE_CONFIG), 1, 'dl.google.com')
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.server_names(), ['cdn.google.com'])

    async def test_haproxy_failure_restores_wildcard_switch_and_profile(self):
        async def failing_ansible(service, config, sni, backend, state='present'):
            self.api.order.append(f'ansible {state}')
            if state == 'present':
                raise RuntimeError('injected haproxy failure')
        with patch('bot.services.infrastructure.provisioning.ansible_bridge', new=failing_ansible), \
             patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'injected haproxy failure'):
                await create_bridge(self.service, SimpleNamespace(id=1, panel_uuid='node', name='node1 (DE)', host_uuid='direct'),
                    self.deployment, dict(BRIDGE_CONFIG), 1, 'dl.google.com')
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.direct_host()['inbound'],
            {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-443'})
        self.assertEqual(self.server_names(), ['cdn.google.com'])
        self.assertEqual([host['uuid'] for host in self.api.hosts], ['direct'])

    async def test_host_failure_rolls_back_everything(self):
        self.api.fail_host_post = True
        with self.assertRaisesRegex(RuntimeError, 'injected host failure'):
            await self.run_bridge()
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.direct_host()['inbound'],
            {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-443'})
        self.assertEqual(self.server_names(), ['cdn.google.com'])
        self.assertEqual(self.ansible_calls[-1], ('dl.google.com', '203.0.113.1:443', 'absent'))
        self.assertEqual([host['uuid'] for host in self.api.hosts], ['direct'])

    async def test_unverified_host_is_deleted_and_everything_rolls_back(self):
        self.api.strip_host_sni = True
        with self.assertRaisesRegex(RuntimeError, 'не подтвердила'):
            await self.run_bridge()
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.direct_host()['inbound'],
            {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-443'})
        self.assertEqual(self.server_names(), ['cdn.google.com'])
        self.assertNotIn('bridge-host', [host['uuid'] for host in self.api.hosts])
        self.assertEqual(self.ansible_calls[-1], ('dl.google.com', '203.0.113.1:443', 'absent'))

    async def test_unconfigured_bridge_is_refused_before_any_change(self):
        with self.assertRaisesRegex(ValueError, 'Мост не настроен'):
            await create_bridge(self.service, SimpleNamespace(id=1, panel_uuid='node', name='node1 (DE)', host_uuid='direct'),
                                self.deployment, {'bridge_address': '', 'bridge_host_public_key': ''}, 1, 'dl.google.com')
        self.assertEqual(self.api.order, [])

    async def test_host_creation_rejects_taken_sni_and_reuses_own_host(self):
        node = SimpleNamespace(id=1, panel_uuid='node', name='node1 (DE)', host_uuid='direct')
        self.api.hosts.append({'uuid': 'foreign', 'address': '203.0.113.9',
            'sni': 'fonts.google.com', 'nodes': ['other-node']})
        with self.assertRaisesRegex(ValueError, 'уже используется'):
            await create_bridge_host(self.api, node, 'bridge.example.net',
                'fonts.google.com', 'profile', 'in-wildcard')
        # A previously created matching host is reused instead of duplicated.
        self.api.hosts = [host for host in self.api.hosts if host['uuid'] != 'foreign']
        self.api.hosts.append({'uuid': 'own-bridge', 'address': 'bridge.example.net',
            'sni': 'fonts.google.com', 'remark': 'node1 (DE) bridge', 'nodes': ['node'],
            'inbound': {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-wildcard'}})
        reused, created = await create_bridge_host(self.api, node, 'bridge.example.net',
            'fonts.google.com', 'profile', 'in-wildcard')
        self.assertEqual(reused, 'own-bridge')
        self.assertFalse(created)


class _SessionContext:
    def __init__(self, session, saved):
        self.session, self.saved = session, saved

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


class BridgeInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_pins_host_key_and_carries_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = SimpleNamespace(settings=SimpleNamespace(
                INFRA_SSH_PRIVATE_KEY='/tmp/automation_key', INFRA_ARTIFACT_DIR=temporary))
            captured = {}

            async def fake_run(playbook, inventory_path):
                captured['playbook'] = str(playbook)
                captured['inventory'] = json.loads(Path(inventory_path).read_text())
                captured['known_hosts'] = (Path(inventory_path).parent / 'known_hosts').read_text()

            with patch('bot.services.infrastructure.provisioning._run_playbook', new=fake_run):
                await ansible_bridge(service, dict(BRIDGE_CONFIG), 'dl.google.com',
                                     '203.0.113.1:443', state='absent')
            host = captured['inventory']['all']['hosts']['node']
            self.assertEqual(host['ansible_host'], 'bridge.example.net')
            self.assertEqual(host['ansible_port'], 22)
            self.assertEqual(host['ansible_user'], 'root')
            self.assertEqual(host['ansible_ssh_private_key_file'], '/tmp/automation_key')
            self.assertEqual(host['bridge_sni'], 'dl.google.com')
            self.assertEqual(host['bridge_backend'], '203.0.113.1:443')
            self.assertEqual(host['bridge_state'], 'absent')
            self.assertIn('bridge.example.net ssh-ed25519 BRIDGEKEY', captured['known_hosts'])
            self.assertTrue(captured['playbook'].endswith('infra/ansible/bridge.yml'))

    async def test_unverified_bridge_is_refused(self):
        service = SimpleNamespace(settings=SimpleNamespace(
            INFRA_SSH_PRIVATE_KEY='/tmp/key', INFRA_ARTIFACT_DIR='.'))
        with self.assertRaisesRegex(ValueError, 'Мост не настроен'):
            await ansible_bridge(service, {'bridge_address': 'bridge.example.net',
                'bridge_ssh_port': 22, 'bridge_ssh_user': 'root', 'bridge_host_public_key': ''},
                'dl.google.com', '203.0.113.1:443')


class BridgeHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=111, user_id=111))
        self.settings = SimpleNamespace(ADMIN_IDS=[111], INFRA_ENABLED=True, DEFAULT_LANGUAGE='en')
        self.i18n = {'current_language': 'en', 'i18n_instance': JsonI18n('locales', default='en')}
        self.session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), get=AsyncMock(return_value=None))
        self.service = SimpleNamespace(api=SimpleNamespace(providers=AsyncMock(return_value=[]),
            call=AsyncMock(return_value={})))
        self.message = SimpleNamespace(from_user=SimpleNamespace(id=111),
            chat=SimpleNamespace(type='private'), answer=AsyncMock(), edit_text=AsyncMock(),
            text='dl.google.com')
        self.callback = SimpleNamespace(from_user=self.message.from_user, message=self.message, answer=AsyncMock())

    async def asyncTearDown(self):
        await self.storage.close()

    async def click(self, payload):
        self.callback.data = payload
        await infrastructure_callback(self.callback, self.state, self.session, self.service, self.settings, self.i18n)

    async def test_bridge_create_asks_sni_and_enqueues_job_with_payload(self):
        self.service.node = AsyncMock(return_value=SimpleNamespace(id=8, name='node1 (DE)', period_days=30))
        self.service.deployment = AsyncMock(return_value=SimpleNamespace(id=5))
        self.service.validate_deployment = AsyncMock()
        job = SimpleNamespace(id=99, payload=None)
        self.service.enqueue = AsyncMock(return_value=job)
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.publish_job', new=AsyncMock()):
            await self.click('infra:bridge_create:8')
            data = await self.state.get_data()
            self.assertEqual(data['flow'], 'bridge_create')
            self.assertEqual(data['node_id'], 8)
            self.assertEqual(await self.state.get_state(), InfrastructureStates.input.state)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        kind = self.service.enqueue.await_args.args[3]
        self.assertEqual(kind, 'bridge_create')
        self.assertEqual(json.loads(job.payload), {'sni': 'dl.google.com'})

    async def test_bridge_create_requires_saved_access(self):
        self.service.node = AsyncMock(return_value=SimpleNamespace(id=8, name='node1 (DE)', period_days=30))
        self.service.deployment = AsyncMock(return_value=None)
        self.service.enqueue = AsyncMock()
        await self.click('infra:bridge_create:8')
        self.service.enqueue.assert_not_awaited()
        self.assertIn('attach', self.message.answer.await_args.args[0].lower())

    async def test_bridge_create_requires_configured_and_verified_bridge(self):
        self.service.node = AsyncMock(return_value=SimpleNamespace(id=8, name='node1 (DE)', period_days=30))
        self.service.deployment = AsyncMock(return_value=SimpleNamespace(id=5))
        self.service.validate_deployment = AsyncMock()
        self.service.enqueue = AsyncMock()
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value={'bridge_address': 'bridge.example.net', 'bridge_host_public_key': ''})):
            await self.click('infra:bridge_create:8')
            await self.state.set_state(InfrastructureStates.input)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        self.service.enqueue.assert_not_awaited()


class BridgeLabelTests(unittest.TestCase):
    def test_operation_and_stage_labels_exist_in_both_locales(self):
        from bot.services.infrastructure.job_messages import kind_label, stage_label
        for language in ('en', 'ru'):
            catalog = JsonI18n('locales', default=language)
            translate = lambda key, **values: catalog.gettext(language, key, **values)
            job = SimpleNamespace(kind='bridge_create', status='running', stage='bridge_profile')
            self.assertNotEqual(kind_label(job, translate), 'infra_operation_bridge_create')
            for stage in ('bridge_profile', 'bridge', 'bridge_host'):
                label = stage_label(SimpleNamespace(stage=stage, status='running'), translate)
                self.assertFalse(label.startswith('infra_'), label)
