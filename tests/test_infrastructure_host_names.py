import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.infrastructure.host_names import country_host_base, country_host_name, existing_created_host


class HostNamesTests(unittest.IsolatedAsyncioTestCase):
    def test_first_and_following_host_names(self):
        self.assertEqual(country_host_name('FI', [], []), '🇫🇮 Finland')
        hosts = [{'remark': '🇫🇮 Finland', 'nodes': ['a']}]
        self.assertEqual(country_host_name('FI', hosts, []), '🇫🇮 Finland 2')
        hosts.append({'remark': '🇫🇮 Finland 4', 'nodes': ['b']})
        self.assertEqual(country_host_name('FI', hosts, []), '🇫🇮 Finland 5')
        self.assertEqual(country_host_base('RU'), '🇷🇺 Russia')
        self.assertEqual(country_host_base('XX'), 'XX')

    def test_existing_custom_names_are_counted_by_node_country(self):
        nodes = [{'uuid': 'first', 'countryCode': 'FI'}, {'uuid': 'other', 'countryCode': 'DE'}]
        hosts = [{'remark': 'legacy node', 'nodes': ['first']},
                 {'remark': 'German node', 'nodes': ['other']}]
        self.assertEqual(country_host_name('FI', hosts, nodes), '🇫🇮 Finland 2')

    def test_retry_finds_previous_host_without_claiming_other_hosts(self):
        node = SimpleNamespace(country='FI', name='node1 (FI)', panel_uuid='new-node')
        deployment = SimpleNamespace(host_address='203.0.113.1')
        host = {'uuid': 'existing', 'nodes': ['new-node'], 'address': '203.0.113.1', 'remark': '🇫🇮 Finland 2'}
        self.assertIs(existing_created_host([host], node, deployment), host)
        self.assertIsNone(existing_created_host([dict(host, address='203.0.113.2')], node, deployment))
        self.assertIsNone(existing_created_host([dict(host, nodes=['other'])], node, deployment))
        with self.assertRaisesRegex(ValueError, 'несколько'):
            existing_created_host([host, dict(host, uuid='second')], node, deployment)

    async def test_provision_posts_country_name_for_next_host(self):
        from bot.services.infrastructure.provisioning import provision
        node = SimpleNamespace(id=1, panel_uuid=None, host_uuid=None, name='node2 (FI)', country='FI', provider_uuid='p')
        deployment = SimpleNamespace(address='203.0.113.2', host_address='203.0.113.2',
            profile_uuid='profile', inbound_uuid='inbound', sni=None)
        nodes = [{'uuid': 'old', 'name': 'node1 (FI)', 'countryCode': 'FI'}]
        hosts = [{'uuid': 'old-host', 'nodes': ['old'], 'remark': '🇫🇮 Finland'}]
        async def call(method, path, payload=None):
            if path == '/nodes':
                return {'uuid': 'new'}
            if method == 'POST' and path == '/hosts':
                return {'uuid': 'new-host'}
            raise AssertionError((method, path))
        api = SimpleNamespace(node_secret=AsyncMock(return_value='secret'), nodes=AsyncMock(return_value=nodes), call=AsyncMock(side_effect=call))
        panel = SimpleNamespace(get_inbounds_by_profile_uuid=AsyncMock(return_value=[{'uuid': 'inbound', 'type': 'vless', 'port': 443}]),
            get_all_hosts=AsyncMock(return_value=hosts))
        @asynccontextmanager
        async def factory():
            yield SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()), commit=AsyncMock())
        service = SimpleNamespace(api=api, panel=panel, factory=factory)
        with patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()), \
             patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock()), \
             patch('bot.services.infrastructure.diagnostics.quick_check', new=AsyncMock(side_effect=[None, RuntimeError('stop after host creation')])):
            with self.assertRaisesRegex(RuntimeError, 'stop after host'):
                await provision(service, node, deployment, {}, 1)
        host_call = next(call for call in api.call.await_args_list if call.args[:2] == ('POST', '/hosts'))
        self.assertEqual(host_call.args[2]['remark'], '🇫🇮 Finland 2')
        self.assertEqual(node.name, 'node2 (FI)')
