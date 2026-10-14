import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.infrastructure.gemini import apply_gemini_route, remove_gemini_route, fork_inbound_tag, detect_panel_selected_gemini


class FakePanel:
    def __init__(self):
        self.source = {'uuid': 'source', 'name': 'Acceptance', 'config': {
            'inbounds': [{'tag': 'other', 'port': 444}, {'tag': 'selected', 'port': 443}],
            'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['selected'], 'outboundTag': 'DIRECT'}]}},
            'inbounds': [{'uuid': 'other-id', 'tag': 'other'}, {'uuid': 'selected-id', 'tag': 'selected'}]}
        self.node = {'uuid': 'node', 'configProfile': {
            'activeConfigProfileUuid': 'source', 'activeInbounds': ['other-id', 'selected-id']}}
        self.host = {'uuid': 'host', 'nodes': ['node'], 'address': '203.0.113.1', 'port': 443,
            'inbound': {'configProfileUuid': 'source', 'configProfileInboundUuid': 'selected-id'}}
        self.modified = None
        self.fail_host_once = False

    async def nodes(self):
        return [copy.deepcopy(self.node)]

    async def profiles(self):
        return [self.source] + ([self.modified] if self.modified else [])

    async def call(self, method, path, payload=None):
        if path == '/hosts' and method == 'GET':
            return [copy.deepcopy(self.host)]
        if path == '/hosts':
            if self.fail_host_once:
                self.fail_host_once = False
                raise RuntimeError('injected host switch failure')
            self.host['inbound'] = copy.deepcopy(payload['inbound'])
            return self.host
        if path == '/internal-squads':
            return {'internalSquads': []}
        if path == '/config-profiles/source':
            return copy.deepcopy(self.source)
        if path == '/config-profiles/modified':
            return copy.deepcopy(self.modified)
        if path == '/config-profiles' and method == 'POST':
            self.modified = dict(payload, uuid='modified', inbounds=[
                {'uuid': 'new-' + entry['tag'], 'tag': entry['tag']} for entry in payload['config']['inbounds']])
            return copy.deepcopy(self.modified)
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
            patch('db.dal.app_settings_dal.upsert_setting', new=AsyncMock(side_effect=save)),
            patch('db.dal.app_settings_dal.get_setting', new=AsyncMock(side_effect=load))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_enable_and_disable_preserve_all_active_inbounds_and_host_fields(self):
        before_node, before_host = copy.deepcopy(self.api.node), copy.deepcopy(self.api.host)
        await apply_gemini_route(self.service, self.session, self.node, SimpleNamespace(), {}, 1, 111)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['new-' + fork_inbound_tag('other', 'selected'), 'new-selected-GEMINI'])
        self.assertEqual(self.api.host['inbound']['configProfileInboundUuid'], 'new-selected-GEMINI')
        self.assertEqual(self.api.modified['config']['routing']['rules'][-1]['inboundTag'], ['selected-GEMINI'])
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
