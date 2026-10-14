import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.infrastructure.gemini import apply_gemini_route, remove_gemini_route, detect_panel_selected_gemini


class FakePanel:
    def __init__(self):
        self.source = {'uuid': 'source', 'name': 'Acceptance', 'config': {
            'inbounds': [{'tag': 'other', 'port': 444}, {'tag': 'selected', 'port': 443}],
            'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['selected'], 'outboundTag': 'DIRECT'}]}},
            'inbounds': [{'uuid': 'other-id', 'tag': 'other'}, {'uuid': 'selected-id', 'tag': 'selected'}]}
        self.node = {'uuid': 'node', 'isConnected': True, 'isConnecting': False, 'configProfile': {
            'activeConfigProfileUuid': 'source', 'activeInbounds': ['other-id', 'selected-id']}}
        self.host = {'uuid': 'host', 'nodes': ['node'], 'address': '203.0.113.1', 'port': 443,
            'inbound': {'configProfileUuid': 'source', 'configProfileInboundUuid': 'selected-id'}}
        self.modified = None
        self.other_hosts = []
        self.restart_node = AsyncMock()
        self.fail_host_once = False

    async def nodes(self):
        return [copy.deepcopy(self.node)]

    async def profiles(self):
        return [self.source] + ([self.modified] if self.modified else [])

    async def call(self, method, path, payload=None):
        if path == '/hosts' and method == 'GET':
            return copy.deepcopy([self.host] + self.other_hosts)
        if path == '/hosts':
            if self.fail_host_once:
                self.fail_host_once = False
                raise RuntimeError('injected host switch failure')
            host = next(h for h in [self.host] + self.other_hosts if h['uuid'] == payload['uuid'])
            host['inbound'] = copy.deepcopy(payload['inbound'])
            return host
        if path == '/internal-squads':
            return {'internalSquads': []}
        if path == '/config-profiles/source':
            return copy.deepcopy(self.source)
        if path == '/config-profiles' and method == 'PATCH':
            old = {entry['tag']: entry['uuid'] for entry in self.source['inbounds']}
            self.source['config'] = copy.deepcopy(payload['config'])
            self.source['inbounds'] = [{'uuid': old.get(entry['tag'], 'new-' + entry['tag']), 'tag': entry['tag']}
                for entry in payload['config']['inbounds']]
            return copy.deepcopy(self.source)
        if path == '/nodes/bulk-actions/profile-modification':
            self.node['configProfile'] = copy.deepcopy(payload['configProfile'])
            return {}
        raise AssertionError((method, path))


class GeminiRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakePanel()
        self.service = SimpleNamespace(api=self.api, scope='scope')
        self.node = SimpleNamespace(id=1, panel_uuid='node')
        self.session = SimpleNamespace(commit=AsyncMock())
        self.state = {}
        async def save(session, key, value, **kwargs):
            self.state[key] = value
        async def load(session, key):
            return SimpleNamespace(value=self.state[key]) if key in self.state else None
        self.patches = [
            patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock()),
            patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()),
            patch('bot.services.infrastructure.gemini.verify_connected', new=AsyncMock()),
            patch('db.dal.app_settings_dal.upsert_setting', new=AsyncMock(side_effect=save)),
            patch('db.dal.app_settings_dal.get_setting', new=AsyncMock(side_effect=load))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_enable_and_disable_preserve_all_active_inbounds_and_host_fields(self):
        before_node, before_host = copy.deepcopy(self.api.node), copy.deepcopy(self.api.host)
        await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['other-id', 'new-selected-GEMINI'])
        self.assertEqual(self.api.host['inbound']['configProfileInboundUuid'], 'new-selected-GEMINI')
        self.assertEqual(self.api.source['config']['routing']['rules'][-1]['inboundTag'], ['selected-GEMINI'])
        await remove_gemini_route(self.service, self.session, self.node, 2, 111)
        self.assertEqual(self.api.node, before_node)
        self.assertEqual(self.api.host, before_host)

    async def test_partial_switch_failure_restores_every_active_inbound(self):
        before_node, before_host = copy.deepcopy(self.api.node), copy.deepcopy(self.api.host)
        self.api.fail_host_once = True
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.assertEqual(self.api.node, before_node)
        self.assertEqual(self.api.host, before_host)
        self.assertEqual(self.state, {})

    async def test_panel_selection_requests_ssh_once_and_can_requeue_after_disable(self):
        await apply_gemini_route(self.service,self.session,self.node,SimpleNamespace(),{},1,111)
        routed_node,routed_host=copy.deepcopy(self.api.node),copy.deepcopy(self.api.host)
        await remove_gemini_route(self.service,self.session,self.node,2,111)
        self.api.node,self.api.host=copy.deepcopy(routed_node),copy.deepcopy(routed_host)
        self.node.name='acceptance'
        self.service.settings=SimpleNamespace(ADMIN_IDS=[111])
        self.service.bot=SimpleNamespace(send_message=AsyncMock())
        self.service.deployment=AsyncMock(return_value=None)
        self.service.enqueue=AsyncMock(return_value=SimpleNamespace(id=55))
        self.session.execute=AsyncMock(return_value=SimpleNamespace(first=lambda:None))
        with patch('bot.services.infrastructure.localization.admin_translator',new=AsyncMock(return_value=lambda key,**kwargs:key)):
            self.assertEqual(await detect_panel_selected_gemini(self.service,self.session,[self.node]),[])
            self.assertEqual(await detect_panel_selected_gemini(self.service,self.session,[self.node]),[])
        self.service.bot.send_message.assert_awaited_once()
        self.service.deployment.return_value=SimpleNamespace(id=1)
        self.assertEqual(await detect_panel_selected_gemini(self.service,self.session,[self.node]),[55])
        self.assertEqual(await detect_panel_selected_gemini(self.service,self.session,[self.node]),[])
        await remove_gemini_route(self.service,self.session,self.node,3,111)
        self.api.node,self.api.host=routed_node,routed_host
        self.assertEqual(await detect_panel_selected_gemini(self.service,self.session,[self.node]),[55])

    async def test_two_hosts_keep_profile_and_other_host_unchanged(self):
        self.api.other_hosts = [{'uuid': 'other-host', 'nodes': ['node'], 'address': '203.0.113.1', 'port': 444,
            'inbound': {'configProfileUuid': 'source', 'configProfileInboundUuid': 'other-id'}}]
        self.node.host_uuid = 'host'
        before = copy.deepcopy(self.api.other_hosts)
        await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.assertEqual(self.api.node['configProfile']['activeConfigProfileUuid'], 'source')
        self.assertEqual(self.api.other_hosts, before)
        self.assertEqual(self.api.source['inbounds'][:2], [{'uuid': 'other-id', 'tag': 'other'}, {'uuid': 'selected-id', 'tag': 'selected'}])
        # A separate active inbound added in-panel after enable must survive disable.
        self.api.node['configProfile']['activeInbounds'].append('extra-id')
        await remove_gemini_route(self.service, self.session, self.node, 2, 111)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['other-id', 'selected-id', 'extra-id'])
        self.assertEqual(self.api.other_hosts, before)

    async def test_shared_source_inbound_is_rejected_before_install(self):
        self.api.other_hosts = [dict(copy.deepcopy(self.api.host), uuid='other-host')]
        self.node.host_uuid = 'host'
        from bot.services.infrastructure.provisioning import ansible
        with self.assertRaisesRegex(ValueError, 'тот же inbound'):
            await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        ansible.assert_not_awaited()
        self.assertEqual(len(self.api.source['inbounds']), 2)

    async def test_independent_host_modifiers_do_not_overwrite_each_other(self):
        self.node.host_uuid = 'host'
        self.api.source['config']['routing']['rules'][0]['inboundTag'] = ['selected', 'other']
        self.api.other_hosts = [{'uuid': 'other-host', 'nodes': ['node'], 'address': '203.0.113.1', 'port': 444,
            'inbound': {'configProfileUuid': 'source', 'configProfileInboundUuid': 'other-id'}}]
        await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.state['infra.gemini.job.scope.2'] = 'other-host'
        await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 2, 111)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['new-other-GEMINI', 'new-selected-GEMINI'])
        from bot.services.infrastructure.gemini import create_or_reuse_gemini_inbound
        await create_or_reuse_gemini_inbound(self.api, 'source', 'selected-id')
        await remove_gemini_route(self.service, self.session, self.node, 3, 111)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['new-other-GEMINI', 'selected-id'])
        self.assertEqual(self.api.other_hosts[0]['inbound']['configProfileInboundUuid'], 'new-other-GEMINI')
        self.state['infra.gemini.job.scope.4'] = 'other-host'
        await remove_gemini_route(self.service, self.session, self.node, 4, 111)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['other-id', 'selected-id'])

    async def test_second_host_panel_selection_enqueues_explicit_host(self):
        self.node.host_uuid = 'host'
        await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.api.other_hosts = [{'uuid': 'other-host', 'nodes': ['node'], 'address': '203.0.113.1', 'port': 444,
            'inbound': {'configProfileUuid': 'source', 'configProfileInboundUuid': 'other-id'}}]
        self.state.clear()
        self.node.name = 'acceptance'
        self.service.settings = SimpleNamespace(ADMIN_IDS=[111])
        self.service.deployment = AsyncMock(return_value=SimpleNamespace(id=1))
        self.service.enqueue = AsyncMock(return_value=SimpleNamespace(id=56))
        self.session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: None))
        self.assertEqual(await detect_panel_selected_gemini(self.service, self.session, [self.node]), [56])
        self.assertEqual(self.service.enqueue.call_args.kwargs['host_uuid'], 'host')

    async def test_existing_variant_must_match_original_and_routes(self):
        from bot.services.infrastructure.gemini import create_or_reuse_gemini_inbound
        await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        await remove_gemini_route(self.service, self.session, self.node, 2, 111)
        sibling = next(i for i in self.api.source['config']['inbounds'] if i['tag'] == 'selected-GEMINI')
        sibling['port'] = 9999
        with self.assertRaisesRegex(ValueError, 'отличается'):
            await create_or_reuse_gemini_inbound(self.api, 'source', 'selected-id')

    async def test_panel_edit_during_install_cancels_without_overwriting_it(self):
        async def edit(*args, **kwargs):
            self.api.node['configProfile']['activeInbounds'].append('manually-added')
        with patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock(side_effect=edit)):
            with self.assertRaisesRegex(ValueError, 'изменены в панели'):
                await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.assertIn('manually-added', self.api.node['configProfile']['activeInbounds'])
        self.assertEqual(len(self.api.source['inbounds']), 2)
        self.assertEqual(self.api.host['inbound']['configProfileInboundUuid'], 'selected-id')

    async def test_restart_failure_rolls_back_node_host_and_state(self):
        before_node, before_host = copy.deepcopy(self.api.node), copy.deepcopy(self.api.host)
        self.api.restart_node.side_effect = [RuntimeError('injected restart failure'), None]
        with self.assertRaisesRegex(RuntimeError, 'injected restart'):
            await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.assertEqual(self.api.node, before_node)
        self.assertEqual(self.api.host, before_host)
        self.assertEqual(self.state, {})

    def test_route_validation_rejects_shadowed_rules_and_nonblocking_udp(self):
        from bot.services.infrastructure.gemini import prepare_gemini_config, has_gemini_routes
        config, tag = prepare_gemini_config(self.api.source['config'], 'selected')
        config['routing']['rules'].insert(0, {'type': 'field', 'outboundTag': 'DIRECT'})
        self.assertFalse(has_gemini_routes(config, tag))
        config['routing']['rules'].pop(0)
        next(o for o in config['outbounds'] if o['tag'] == 'BLOCK')['protocol'] = 'freedom'
        self.assertFalse(has_gemini_routes(config, tag))


class GeminiConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_requires_three_stable_observations(self):
        from bot.services.infrastructure.gemini import verify_connected
        ready = {'uuid': 'node', 'isConnected': True, 'isConnecting': False}
        api = SimpleNamespace(nodes=AsyncMock(side_effect=[
            [dict(ready, isConnecting=True)], [ready], [ready], [ready]]))
        with patch('asyncio.sleep', new=AsyncMock()):
            await verify_connected(api, 'node')
        self.assertEqual(api.nodes.await_count, 4)

    async def test_connection_does_not_treat_missing_flags_as_success(self):
        from bot.services.infrastructure.gemini import verify_connected
        api = SimpleNamespace(nodes=AsyncMock(return_value=[{'uuid': 'node'}]))
        with patch('asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'не подключилось'):
                await verify_connected(api, 'node')
