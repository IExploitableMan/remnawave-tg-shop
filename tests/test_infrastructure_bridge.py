"""Bridge (haproxy SNI fronting) creation, removal and handler flow.

The bridge node — the infra node deployed at the bridge address — owns the
VLESS-WILDCARD listener: the bot activates it there and keeps its serverNames
equal to the whole SNI routing table. Bridge hosts point at the bridge address
with the wildcard inbound and an SNI override; the bridged node's own panel
state is never modified. Passthrough SNIs (dl.google.com) live in the bot
settings and must survive bot operations.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from bot.handlers.admin.infrastructure import InfrastructureStates, infrastructure_callback, infrastructure_input
from bot.middlewares.i18n import JsonI18n
from bot.services.infrastructure.bridge import (bridge_entries, change_wildcard_dest, create_bridge,
    create_bridge_host, remove_bridge, repoint_bridge_hosts, sync_wildcard_snis)
from bot.services.infrastructure.provisioning import ansible_bridge

PASSTHROUGH_SNIS = ['dl.google.com', 'fonts.google.com']
MASK_ROUTE = {'sni': 'dl.google.com', 'backend': '127.0.0.1:8443'}


class FakeBridgePanel:
    def __init__(self):
        # VLESS-WILDCARD belongs to the bridge node: its own keypair and the
        # global SNI list, including passthrough entries.
        inbounds = [
            {'tag': 'VLESS-443', 'port': 443, 'streamSettings': {'realitySettings': {
                'serverNames': ['www.google.com'], 'dest': 'www.google.com:443'}}},
            {'tag': 'VLESS-WILDCARD', 'port': 443, 'streamSettings': {'realitySettings': {
                'serverNames': list(PASSTHROUGH_SNIS), 'target': 'dl.google.com:443',
                'privateKey': 'BRIDGE-KEY'}}}]
        self.profile = {'uuid': 'profile', 'name': 'Common',
            'config': {'inbounds': inbounds, 'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}]},
            'inbounds': [{'uuid': 'in-443', 'tag': 'VLESS-443'},
                         {'uuid': 'in-wildcard', 'tag': 'VLESS-WILDCARD'}]}
        self.restart_node = AsyncMock()
        self.node = {'uuid': 'node', 'isConnected': True, 'isConnecting': False, 'hostUuid': 'direct', 'configProfile': {
            'activeConfigProfileUuid': 'profile', 'activeInbounds': ['in-443']}}
        self.bridge_node = {'uuid': 'bridge-node', 'isConnected': True, 'isConnecting': False,
            'configProfile': {'activeConfigProfileUuid': 'profile', 'activeInbounds': ['in-wildcard']}}
        self.hosts = [{'uuid': 'direct', 'address': '203.0.113.1', 'port': 443, 'nodes': ['node'],
                       'remark': 'node1 (DE)', 'sni': 'www.google.com',
                       'inbound': {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-443'}}]
        self.squads = [{'uuid': 'squad-1', 'inbounds': ['in-443']}]
        self.order = []
        self.fail_host_post = False

    async def nodes(self):
        return [copy.deepcopy(self.node), copy.deepcopy(self.bridge_node)]

    async def call(self, method, path, payload=None):
        self.order.append(f'{method} {path}')
        if path == '/config-profiles/profile' and method == 'GET':
            return copy.deepcopy(self.profile)
        if path == '/config-profiles' and method == 'PATCH':
            uuids = {entry['tag']: entry['uuid'] for entry in self.profile['inbounds']}
            self.profile['config'] = copy.deepcopy(payload['config'])
            self.profile['inbounds'] = [{'tag': entry['tag'], 'uuid': uuids.get(entry['tag'], 'in-' + entry['tag'])}
                for entry in payload['config']['inbounds']]
            return {}
        if path == '/internal-squads' and method == 'GET':
            return {'internalSquads': copy.deepcopy(self.squads)}
        if path == '/internal-squads' and method == 'PATCH':
            squad = next(item for item in self.squads if item['uuid'] == payload['uuid'])
            squad['inbounds'] = list(payload['inbounds'])
            return copy.deepcopy(squad)
        if path == '/hosts' and method == 'GET':
            return copy.deepcopy(self.hosts)
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
            target = self.node if payload['uuids'] == ['node'] else self.bridge_node
            target['configProfile'] = copy.deepcopy(payload['configProfile'])
            return {}
        raise AssertionError((method, path))


BRIDGE_CONFIG = {'bridge_address': 'bridge.example.net', 'bridge_ssh_port': 22,
                 'bridge_ssh_user': 'root', 'bridge_host_public_key': 'ssh-ed25519 BRIDGEKEY',
                 'bridge_extra_routes': [], 'bridge_wildcard_backend': '127.0.0.1:8443'}

NODE = dict(id=1, panel_uuid='node', name='node1 (DE)', host_uuid='direct')
CLIENT_NODE = SimpleNamespace(id=1, panel_uuid='node', name='node1 (DE)', host_uuid='direct')
BRIDGE_INFRA = SimpleNamespace(id=9, panel_uuid='bridge-node', name='node9 (RU)', host_uuid=None)

WILDCARD_BINDING = {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'in-wildcard'}


def as_node(**overrides):
    return SimpleNamespace(**dict(NODE, **overrides))


class BridgeFlowBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeBridgePanel()
        self.deployment = SimpleNamespace(id=5, address='203.0.113.1',
            profile_uuid='profile', inbound_uuid='in-443')
        self.saved = SimpleNamespace(profile_uuid='profile', inbound_uuid='in-443')
        self.service = SimpleNamespace(api=self.api, factory=self.fake_factory, scope='bridge-test')
        self.persisted = {}
        self.deleted = []
        self.deployments = {1: self.deployment, 9: SimpleNamespace(id=6, address='bridge.example.net')}
        async def save(session, key, value, **kwargs): self.persisted[key] = value
        async def load(session, key):
            value = self.persisted.get(key)
            return SimpleNamespace(value=value) if value is not None else None
        async def drop(session, key):
            self.deleted.append(key)
            self.persisted.pop(key, None)
        for patcher in [patch('db.dal.app_settings_dal.upsert_setting', new=AsyncMock(side_effect=save)),
                patch('db.dal.app_settings_dal.get_setting', new=AsyncMock(side_effect=load)),
                patch('db.dal.app_settings_dal.delete_setting', new=AsyncMock(side_effect=drop)),
                patch('db.dal.app_settings_dal.get_settings_map', new=AsyncMock(
                    side_effect=lambda session: dict(self.persisted))),
                patch('bot.services.infrastructure.bridge.verify_connected', new=AsyncMock())]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service.nodes = AsyncMock(return_value=[CLIENT_NODE, BRIDGE_INFRA])
        self.service.deployment = AsyncMock(
            side_effect=lambda session, node_id: self.deployments.get(int(node_id)))
        self.service.node = AsyncMock(side_effect=lambda session, node_id:
            CLIENT_NODE if int(node_id) == 1 else BRIDGE_INFRA)
        self.ansible_calls = []

    def fake_factory(self):
        session = SimpleNamespace(
            get=AsyncMock(return_value=self.saved), commit=AsyncMock())
        return _SessionContext(session, self.saved)

    def ansible_patch(self):
        async def fake_ansible(service, config, routes):
            self.ansible_calls.append([dict(route) for route in routes])
            self.api.order.append('ansible')
        return patch('bot.services.infrastructure.provisioning.ansible_bridge', new=fake_ansible)

    async def run_bridge(self, sni='cdn.google.com', config=None):
        with self.ansible_patch(), \
             patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()) as staged:
            await create_bridge(self.service, as_node(), self.deployment,
                config or dict(BRIDGE_CONFIG), 1, sni)
        return staged

    async def run_removal(self, config=None):
        with self.ansible_patch(), \
             patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()):
            await remove_bridge(self.service, as_node(), self.deployment,
                config or dict(BRIDGE_CONFIG), 2)

    def wildcard_names(self):
        inbound = next(entry for entry in self.api.profile['config']['inbounds']
            if entry['tag'] == 'VLESS-WILDCARD')
        return inbound['streamSettings']['realitySettings']['serverNames']

    def source_names(self):
        inbound = next(entry for entry in self.api.profile['config']['inbounds']
            if entry['tag'] == 'VLESS-443')
        return inbound['streamSettings']['realitySettings']['serverNames']

    def direct_host(self):
        return next(host for host in self.api.hosts if host['uuid'] == 'direct')

    def bridge_host(self):
        return next(host for host in self.api.hosts if host['uuid'] == 'bridge-host')

    def registered(self):
        return json.loads(self.persisted.get('infra.bridge.bridge-test.1', 'null'))


class BridgeCreationTests(BridgeFlowBase):
    async def test_creation_switches_node_onto_wildcard_and_wires_route(self):
        await self.run_bridge()
        order = self.api.order
        self.assertLess(order.index('PATCH /config-profiles'),
                        order.index('POST /nodes/bulk-actions/profile-modification'))
        self.assertLess(order.index('POST /nodes/bulk-actions/profile-modification'), order.index('ansible'))
        self.assertLess(order.index('ansible'), order.index('POST /hosts'))
        # The render carries the full desired table: the bridge route points
        # at the bridged node itself, the mask domain is routed automatically.
        self.assertEqual(self.ansible_calls[-1],
            [MASK_ROUTE, {'sni': 'cdn.google.com', 'backend': '203.0.113.1:443'}])
        # The wildcard list is the global SNI list; the source inbound is untouched.
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS + ['cdn.google.com'])
        self.assertEqual(self.source_names(), ['www.google.com'])
        # The bridged node itself terminates the wildcard on its own 443.
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-wildcard'])
        self.api.restart_node.assert_awaited_once_with('node', force=True)
        self.assertEqual(self.direct_host()['inbound'], WILDCARD_BINDING)
        self.assertEqual(self.api.squads[0]['inbounds'], ['in-443', 'in-wildcard'])
        bridge = self.bridge_host()
        self.assertEqual(bridge['address'], 'bridge.example.net')
        self.assertEqual(bridge['sni'], 'cdn.google.com')
        self.assertEqual(bridge['nodes'], ['node'])
        self.assertEqual(bridge['inbound'], WILDCARD_BINDING)
        self.assertEqual(bridge['remark'], 'node1 (DE) bridge')
        state = self.registered()
        self.assertEqual(state['sni'], 'cdn.google.com')
        self.assertTrue(state['sni_added'])
        self.assertEqual(state['wildcard_uuid'], 'in-wildcard')
        self.assertEqual(state['previous_inbounds'], ['in-443'])
        self.assertEqual(state['previous_host_inbound']['configProfileInboundUuid'], 'in-443')
        self.assertEqual(state['previous_deployment'], {'profile_uuid': 'profile', 'inbound_uuid': 'in-443'})
        # The deployment card follows the wildcard listener the node now serves.
        self.assertEqual((self.saved.profile_uuid, self.saved.inbound_uuid), ('profile', 'in-wildcard'))
        # The bridge node itself is never switched or restarted.
        self.assertEqual(self.api.bridge_node['configProfile']['activeInbounds'], ['in-wildcard'])

    async def test_bridge_node_is_never_switched_or_restarted(self):
        self.api.bridge_node['configProfile']['activeInbounds'] = []
        await self.run_bridge()
        # haproxy owns 443 on the bridge: its node keeps whatever it had and
        # is never restarted; only the client node is switched.
        self.assertEqual(self.api.bridge_node['configProfile']['activeInbounds'], [])
        self.api.restart_node.assert_awaited_once_with('node', force=True)
        self.assertEqual(self.api.order.count('POST /nodes/bulk-actions/profile-modification'), 1)

    async def test_passthrough_sni_is_reused_without_relisting(self):
        await self.run_bridge(sni='fonts.google.com')
        self.assertNotIn('PATCH /config-profiles', self.api.order)
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS)
        self.assertEqual(self.registered()['sni_added'], False)
        self.assertEqual(self.bridge_host()['sni'], 'fonts.google.com')

    async def test_missing_wildcard_inbound_is_refused_before_any_change(self):
        self.api.profile['config']['inbounds'] = [self.api.profile['config']['inbounds'][0]]
        self.api.profile['inbounds'] = [self.api.profile['inbounds'][0]]
        with self.assertRaisesRegex(ValueError, 'VLESS-WILDCARD'):
            await self.run_bridge()
        self.assertEqual(self.ansible_calls, [])
        self.assertNotIn('POST /hosts', self.api.order)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])

    async def test_gemini_enabled_on_direct_host_is_refused(self):
        self.persisted['infra.gemini.bridge-test.1.direct'] = json.dumps({'enabled': True})
        with self.assertRaisesRegex(ValueError, 'Gemini'):
            await self.run_bridge()
        self.assertEqual(self.ansible_calls, [])
        self.assertNotIn('POST /hosts', self.api.order)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])

    async def test_second_bridge_with_different_sni_is_refused(self):
        self.api.hosts.append({'uuid': 'other-bridge', 'address': 'bridge.example.net',
            'sni': 'fonts.google.com', 'nodes': ['node'], 'remark': 'node1 (DE) bridge'})
        with self.assertRaisesRegex(ValueError, 'уже есть мост'):
            await self.run_bridge(sni='cdn.google.com')
        self.assertEqual(self.ansible_calls, [])

    async def test_duplicate_sni_route_is_refused(self):
        config = dict(BRIDGE_CONFIG, bridge_extra_routes=['cdn.google.com 1.2.3.4:443'])
        with self.assertRaisesRegex(ValueError, 'уже используется другим маршрутом'):
            await self.run_bridge(sni='cdn.google.com', config=config)
        self.assertEqual(self.ansible_calls, [])
        self.assertNotIn('POST /hosts', self.api.order)

    async def test_unrelated_active_inbound_and_host_are_preserved(self):
        self.api.profile['config']['inbounds'].append({'tag': 'OTHER', 'port': 8443})
        self.api.profile['inbounds'].append({'tag': 'OTHER', 'uuid': 'other-inbound'})
        self.api.node['configProfile']['activeInbounds'].append('other-inbound')
        other = {'uuid': 'other-host', 'nodes': ['node'], 'address': '203.0.113.1', 'port': 8443,
            'inbound': {'configProfileUuid': 'profile', 'configProfileInboundUuid': 'other-inbound'}}
        self.api.hosts.append(copy.deepcopy(other))
        await self.run_bridge()
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-wildcard', 'other-inbound'])
        self.assertEqual(next(h for h in self.api.hosts if h['uuid'] == 'other-host'), other)

    async def test_render_includes_extras_and_other_bridges(self):
        self.persisted['infra.bridge.bridge-test.9'] = json.dumps({'sni': 'fonts.google.com'})
        config = dict(BRIDGE_CONFIG, bridge_extra_routes=['dl.google.com 127.0.0.1:8443'])
        await self.run_bridge(sni='cdn.google.com', config=config)
        self.assertEqual(self.ansible_calls[-1], [
            {'sni': 'dl.google.com', 'backend': '127.0.0.1:8443'},
            {'sni': 'fonts.google.com', 'backend': 'bridge.example.net:443'},
            {'sni': 'cdn.google.com', 'backend': '203.0.113.1:443'}])

    async def test_haproxy_failure_rolls_back_everything(self):
        async def failing(service, config, routes):
            self.api.order.append('ansible')
            self.ansible_calls.append([dict(route) for route in routes])
            if any(route['sni'] == 'cdn.google.com' for route in routes):
                raise RuntimeError('injected haproxy failure')
        with patch('bot.services.infrastructure.provisioning.ansible_bridge', new=failing), \
             patch('bot.services.infrastructure.provisioning.stage', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'injected haproxy failure'):
                await create_bridge(self.service, as_node(), self.deployment, dict(BRIDGE_CONFIG), 1, 'cdn.google.com')
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.direct_host()['inbound']['configProfileInboundUuid'], 'in-443')
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS)
        self.assertEqual(self.api.squads[0]['inbounds'], ['in-443'])
        self.assertEqual([host['uuid'] for host in self.api.hosts], ['direct'])
        # The rollback re-renders the table without the failed route.
        self.assertEqual(self.ansible_calls[-1], [MASK_ROUTE])
        self.assertIsNone(self.registered())

    async def test_host_failure_rolls_back_everything(self):
        self.api.fail_host_post = True
        with self.assertRaisesRegex(RuntimeError, 'injected host failure'):
            await self.run_bridge()
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.direct_host()['inbound']['configProfileInboundUuid'], 'in-443')
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS)
        self.assertEqual(self.api.squads[0]['inbounds'], ['in-443'])
        self.assertEqual([host['uuid'] for host in self.api.hosts], ['direct'])
        self.assertEqual(self.ansible_calls[-1], [MASK_ROUTE])
        self.assertIsNone(self.registered())

    async def test_unconfigured_bridge_is_refused_before_any_change(self):
        with self.assertRaisesRegex(ValueError, 'Мост не настроен'):
            await create_bridge(self.service, as_node(), self.deployment,
                {'bridge_address': '', 'bridge_host_public_key': ''}, 1, 'cdn.google.com')
        self.assertEqual(self.api.order, [])

    async def test_retry_with_orphaned_host_heals_the_route(self):
        # A host from a failed run whose registration never saved: the retry
        # must still render the route, otherwise haproxy cuts the SNI off
        # while the panel reports a successful bridge.
        self.api.hosts.append({'uuid': 'bridge-host', 'address': 'bridge.example.net',
            'sni': 'cdn.google.com', 'remark': 'node1 (DE) bridge', 'nodes': ['node'],
            'inbound': dict(WILDCARD_BINDING)})
        await self.run_bridge()
        self.assertEqual(self.ansible_calls[-1],
            [MASK_ROUTE, {'sni': 'cdn.google.com', 'backend': '203.0.113.1:443'}])
        self.assertNotIn('POST /hosts', self.api.order)
        self.assertEqual(self.registered()['bridge_host_uuid'], 'bridge-host')

    async def test_host_creation_rejects_taken_sni_and_reuses_own_host(self):
        self.api.hosts.append({'uuid': 'foreign', 'address': '203.0.113.9',
            'sni': 'fonts.google.com', 'nodes': ['other-node']})
        with self.assertRaisesRegex(ValueError, 'уже используется'):
            await create_bridge_host(self.api, as_node(), 'bridge.example.net',
                'fonts.google.com', WILDCARD_BINDING)
        # A previously created matching host is reused instead of duplicated.
        self.api.hosts = [host for host in self.api.hosts if host['uuid'] != 'foreign']
        self.api.hosts.append({'uuid': 'own-bridge', 'address': 'bridge.example.net',
            'sni': 'fonts.google.com', 'remark': 'node1 (DE) bridge', 'nodes': ['node'],
            'inbound': dict(WILDCARD_BINDING)})
        reused, created = await create_bridge_host(self.api, as_node(), 'bridge.example.net',
            'fonts.google.com', WILDCARD_BINDING)
        self.assertEqual(reused, 'own-bridge')
        self.assertFalse(created)


class BridgeRemovalTests(BridgeFlowBase):
    async def test_remove_reverses_creation_in_order(self):
        await self.run_bridge()
        await self.run_removal()
        removal = self.api.order[self.api.order.index('DELETE /hosts/bridge-host'):]
        self.assertLess(removal.index('ansible'), removal.index('PATCH /hosts'))
        self.assertLess(removal.index('PATCH /hosts'),
                        removal.index('POST /nodes/bulk-actions/profile-modification'))
        self.assertNotIn('bridge-host', [host['uuid'] for host in self.api.hosts])
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.direct_host()['inbound']['configProfileInboundUuid'], 'in-443')
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS)
        self.assertEqual(self.api.squads[0]['inbounds'], ['in-443'])
        self.assertEqual((self.saved.profile_uuid, self.saved.inbound_uuid), ('profile', 'in-443'))
        self.assertIn('infra.bridge.bridge-test.1', self.deleted)
        self.assertIn('infra.gemini.bridge-test.1.bridge-host', self.deleted)
        # The render excludes the removed bridge; only the mask route stays.
        self.assertEqual(self.ansible_calls[-1], [MASK_ROUTE])

    async def test_remove_keeps_passthrough_sni_listed(self):
        await self.run_bridge(sni='fonts.google.com')
        await self.run_removal()
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS)
        self.assertEqual(self.api.node['configProfile']['activeInbounds'], ['in-443'])
        self.assertEqual(self.api.squads[0]['inbounds'], ['in-443'])
        self.assertIn('infra.bridge.bridge-test.1', self.deleted)

    async def test_remove_keeps_sni_used_by_another_bridge(self):
        await self.run_bridge(sni='cdn.google.com')
        self.persisted['infra.bridge.bridge-test.9'] = json.dumps({'sni': 'cdn.google.com'})
        await self.run_removal()
        self.assertIn('cdn.google.com', self.wildcard_names())
        # The other node's route survives the re-render, pointing at its node.
        self.assertEqual(self.ansible_calls[-1],
            [{'sni': 'cdn.google.com', 'backend': 'bridge.example.net:443'}, MASK_ROUTE])
        self.assertIn('infra.bridge.bridge-test.1', self.deleted)

    async def test_remove_keeps_extra_route_sni(self):
        await self.run_bridge(sni='cdn.google.com')
        config = dict(BRIDGE_CONFIG, bridge_extra_routes=['cdn.google.com 127.0.0.1:8443'])
        await self.run_removal(config=config)
        self.assertIn('cdn.google.com', self.wildcard_names())
        self.assertEqual(self.ansible_calls[-1],
            [{'sni': 'cdn.google.com', 'backend': '127.0.0.1:8443'}, MASK_ROUTE])
        self.assertIn('infra.bridge.bridge-test.1', self.deleted)

    async def test_removal_is_refused_while_gemini_is_enabled(self):
        await self.run_bridge()
        self.persisted['infra.gemini.bridge-test.1.direct'] = json.dumps({'enabled': True})
        with self.assertRaisesRegex(ValueError, 'Gemini'):
            await self.run_removal()
        self.assertIn('bridge-host', [host['uuid'] for host in self.api.hosts])
        self.assertEqual(len(self.ansible_calls), 1)
        self.assertNotIn('infra.bridge.bridge-test.1', self.deleted)

    async def test_removal_without_registration_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'нет зарегистрированного моста'):
            await self.run_removal()
        self.assertEqual(self.api.order, [])

    async def test_removal_after_manual_host_delete_still_cleans_up(self):
        await self.run_bridge()
        self.api.hosts = [self.direct_host()]
        await self.run_removal()
        self.assertEqual(self.ansible_calls[-1], [MASK_ROUTE])
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS)
        self.assertIn('infra.bridge.bridge-test.1', self.deleted)

    async def test_bridge_entries_lists_registered_nodes_sorted_by_name(self):
        self.persisted['infra.bridge.bridge-test.9'] = json.dumps({'sni': 'fonts.google.com'})
        self.persisted['infra.bridge.bridge-test.1'] = json.dumps({'sni': 'dl.google.com'})
        self.persisted['infra.bridge.bridge-test.2'] = ''
        async def node(session, node_id):
            names = {'1': 'node1 (DE)', '9': 'node9 (RU)'}
            return SimpleNamespace(id=int(node_id), name=names[node_id])
        self.service.node = AsyncMock(side_effect=node)
        entries = await bridge_entries(self.service, None)
        self.assertEqual(entries, [(1, 'node1 (DE)', 'dl.google.com'), (9, 'node9 (RU)', 'fonts.google.com')])


class BridgeAddressAndSyncTests(BridgeFlowBase):
    async def test_bridge_address_change_repoints_bridge_hosts(self):
        await self.run_bridge()
        self.bridge_host()['address'] = 'old.example.net'
        updated = await repoint_bridge_hosts(self.service, dict(BRIDGE_CONFIG))
        self.assertEqual(updated, 1)
        self.assertEqual(self.bridge_host()['address'], 'bridge.example.net')
        self.assertIn('PATCH /hosts', self.api.order)
        # Idempotent: nothing left to repoint.
        updated = await repoint_bridge_hosts(self.service, dict(BRIDGE_CONFIG))
        self.assertEqual(updated, 0)

    async def test_sync_adds_missing_table_snis_to_wildcard(self):
        changed = await sync_wildcard_snis(self.api, 'profile', ['dl.google.com', 'cdn.google.com'])
        self.assertTrue(changed)
        self.assertEqual(self.wildcard_names(), PASSTHROUGH_SNIS + ['cdn.google.com'])
        self.assertIn('PATCH /config-profiles', self.api.order)
        changed = await sync_wildcard_snis(self.api, 'profile', ['dl.google.com', 'cdn.google.com'])
        self.assertFalse(changed)

    async def test_mask_domain_is_routed_automatically(self):
        from bot.services.infrastructure.bridge import desired_routes
        routes = await desired_routes(self.service, dict(BRIDGE_CONFIG))
        self.assertEqual(routes, [MASK_ROUTE])
        # A manual extra for the same domain does not duplicate the route.
        config = dict(BRIDGE_CONFIG, bridge_extra_routes=['dl.google.com 127.0.0.1:8443'])
        routes = await desired_routes(self.service, config)
        self.assertEqual(routes, [{'sni': 'dl.google.com', 'backend': '127.0.0.1:8443'}])

    async def test_no_mask_route_without_reality_target(self):
        from bot.services.infrastructure.bridge import desired_routes
        wildcard = next(entry for entry in self.api.profile['config']['inbounds']
            if entry['tag'] == 'VLESS-WILDCARD')
        reality = wildcard['streamSettings']['realitySettings']
        reality.pop('target', None)
        reality.pop('dest', None)
        routes = await desired_routes(self.service, dict(BRIDGE_CONFIG))
        self.assertEqual(routes, [])

    async def test_validate_route_brackets_ipv6_backends(self):
        from bot.services.infrastructure.settings import validate_backend, validate_route
        self.assertEqual(validate_route('dl.google.com [2001:db8::1]:443'),
            ('dl.google.com', '[2001:db8::1]:443'))
        self.assertEqual(validate_route('dl.google.com 1.2.3.4:443'),
            ('dl.google.com', '1.2.3.4:443'))
        self.assertEqual(validate_backend('[2001:db8::1]:8443'), '[2001:db8::1]:8443')
        self.assertEqual(validate_backend('10.0.0.5:8443'), '10.0.0.5:8443')


class BridgeWildcardChangeTests(BridgeFlowBase):
    async def test_change_wildcard_dest_updates_without_restart(self):
        with self.ansible_patch(), \
             patch('bot.services.infrastructure.bridge._sni_supported', new=AsyncMock(return_value=True)):
            verified = await change_wildcard_dest(self.service, dict(BRIDGE_CONFIG), 'cloudflare.com')
        self.assertEqual(verified, len(PASSTHROUGH_SNIS))
        wildcard = next(entry for entry in self.api.profile['config']['inbounds']
            if entry['tag'] == 'VLESS-WILDCARD')
        self.assertEqual(wildcard['streamSettings']['realitySettings']['target'], 'cloudflare.com:443')
        # The domain lands in serverNames and is routed to the local backend.
        self.assertIn('cloudflare.com', self.wildcard_names())
        self.assertEqual(self.ansible_calls[-1],
            [{'sni': 'cloudflare.com', 'backend': '127.0.0.1:8443'}])
        self.assertIn('PATCH /config-profiles', self.api.order)
        # The bridge node is never restarted.
        self.api.restart_node.assert_not_awaited()

    async def test_change_wildcard_dest_aborts_when_certificate_does_not_cover(self):
        with self.ansible_patch(), \
             patch('bot.services.infrastructure.bridge._sni_supported', new=AsyncMock(return_value=False)):
            with self.assertRaisesRegex(ValueError, 'не покрывает'):
                await change_wildcard_dest(self.service, dict(BRIDGE_CONFIG), 'cloudflare.com')
        self.assertNotIn('PATCH /config-profiles', self.api.order)
        self.assertEqual(self.ansible_calls, [])
        self.api.restart_node.assert_not_awaited()
        wildcard = next(entry for entry in self.api.profile['config']['inbounds']
            if entry['tag'] == 'VLESS-WILDCARD')
        self.assertEqual(wildcard['streamSettings']['realitySettings']['target'], 'dl.google.com:443')
        self.assertNotIn('cloudflare.com', self.wildcard_names())

    def test_certificate_name_covers_tls_wildcard_semantics(self):
        from bot.services.infrastructure.bridge import _certificate_name_covers
        self.assertTrue(_certificate_name_covers('a.google.com', '*.google.com'))
        self.assertTrue(_certificate_name_covers('A.Google.COM', '*.google.com'))
        self.assertFalse(_certificate_name_covers('a.b.google.com', '*.google.com'))
        self.assertFalse(_certificate_name_covers('google.com', '*.google.com'))
        self.assertTrue(_certificate_name_covers('dl.google.com', 'dl.google.com'))
        self.assertFalse(_certificate_name_covers('dl.google.com', 'fonts.google.com'))


class _SessionContext:
    def __init__(self, session, saved):
        self.session, self.saved = session, saved

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


class BridgeInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_pins_host_key_and_carries_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = SimpleNamespace(settings=SimpleNamespace(
                INFRA_SSH_PRIVATE_KEY='/tmp/automation_key', INFRA_ARTIFACT_DIR=temporary))
            captured = {}

            async def fake_run(playbook, inventory_path):
                captured['playbook'] = str(playbook)
                captured['inventory'] = json.loads(Path(inventory_path).read_text())
                captured['known_hosts'] = (Path(inventory_path).parent / 'known_hosts').read_text()

            routes = [{'sni': 'dl.google.com', 'backend': '127.0.0.1:8443'},
                      {'sni': 'cdn.google.com', 'backend': '203.0.113.1:443'}]
            with patch('bot.services.infrastructure.provisioning._run_playbook', new=fake_run):
                await ansible_bridge(service, dict(BRIDGE_CONFIG), routes)
            host = captured['inventory']['all']['hosts']['node']
            self.assertEqual(host['ansible_host'], 'bridge.example.net')
            self.assertEqual(host['ansible_port'], 22)
            self.assertEqual(host['ansible_user'], 'root')
            self.assertEqual(host['ansible_ssh_private_key_file'], '/tmp/automation_key')
            self.assertEqual(host['bridge_routes'], routes)
            self.assertIn('bridge.example.net ssh-ed25519 BRIDGEKEY', captured['known_hosts'])
            self.assertTrue(captured['playbook'].endswith('infra/ansible/bridge.yml'))

    async def test_unverified_bridge_is_refused(self):
        service = SimpleNamespace(settings=SimpleNamespace(
            INFRA_SSH_PRIVATE_KEY='/tmp/key', INFRA_ARTIFACT_DIR='.'))
        with self.assertRaisesRegex(ValueError, 'Мост не настроен'):
            await ansible_bridge(service, {'bridge_address': 'bridge.example.net',
                'bridge_ssh_port': 22, 'bridge_ssh_user': 'root', 'bridge_host_public_key': ''},
                [{'sni': 'cdn.google.com', 'backend': '203.0.113.1:443'}])


class BridgeHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=111, user_id=111))
        self.settings = SimpleNamespace(ADMIN_IDS=[111], INFRA_ENABLED=True, DEFAULT_LANGUAGE='en')
        self.i18n = {'current_language': 'en', 'i18n_instance': JsonI18n('locales', default='en')}
        self.session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), get=AsyncMock(return_value=None))
        self.service = SimpleNamespace(api=SimpleNamespace(providers=AsyncMock(return_value=[]),
            call=AsyncMock(return_value={})), scope='bridge-test')
        self.message = SimpleNamespace(from_user=SimpleNamespace(id=111),
            chat=SimpleNamespace(type='private'), answer=AsyncMock(), edit_text=AsyncMock(),
            text='cdn.google.com')
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
            await self.click('infra:bridge_pick:8')
            data = await self.state.get_data()
            self.assertEqual(data['flow'], 'bridge_create')
            self.assertEqual(data['node_id'], 8)
            self.assertEqual(await self.state.get_state(), InfrastructureStates.input.state)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        kind = self.service.enqueue.await_args.args[3]
        self.assertEqual(kind, 'bridge_create')
        self.assertEqual(json.loads(job.payload), {'sni': 'cdn.google.com'})

    async def test_bridge_create_requires_saved_access(self):
        self.service.node = AsyncMock(return_value=SimpleNamespace(id=8, name='node1 (DE)', period_days=30))
        self.service.deployment = AsyncMock(return_value=None)
        self.service.enqueue = AsyncMock()
        await self.click('infra:bridge_pick:8')
        self.service.enqueue.assert_not_awaited()
        self.assertIn('attach', self.message.answer.await_args.args[0].lower())

    async def test_bridge_create_requires_configured_and_verified_bridge(self):
        self.service.node = AsyncMock(return_value=SimpleNamespace(id=8, name='node1 (DE)', period_days=30))
        self.service.deployment = AsyncMock(return_value=SimpleNamespace(id=5))
        self.service.validate_deployment = AsyncMock()
        self.service.enqueue = AsyncMock()
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value={'bridge_address': 'bridge.example.net', 'bridge_host_public_key': ''})):
            await self.click('infra:bridge_pick:8')
            await self.state.set_state(InfrastructureStates.input)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        self.service.enqueue.assert_not_awaited()

    async def test_bridges_menu_lists_registered_bridge(self):
        self.service.node = AsyncMock(return_value=SimpleNamespace(id=8, name='node1 (DE)'))
        settings_map = {'infra.bridge.bridge-test.8': json.dumps({'sni': 'cdn.google.com'})}
        with patch('db.dal.app_settings_dal.get_settings_map', new=AsyncMock(return_value=settings_map)), \
             patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                 return_value=dict(BRIDGE_CONFIG))):
            await self.click('infra:bridges')
        markup = self.message.edit_text.await_args.kwargs['reply_markup']
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertTrue(any('node1 (DE)' in text and 'cdn.google.com' in text for text in labels), labels)

    async def test_bridge_remove_confirm_enqueues_bridge_delete(self):
        self.service.node = AsyncMock(return_value=SimpleNamespace(id=8, name='node1 (DE)'))
        self.service.deployment = AsyncMock(return_value=SimpleNamespace(id=5))
        self.service.validate_deployment = AsyncMock()
        self.service.enqueue = AsyncMock(return_value=SimpleNamespace(id=99))
        settings_map = {'infra.bridge.bridge-test.8': json.dumps({'sni': 'cdn.google.com'})}
        with patch('db.dal.app_settings_dal.get_setting', new=AsyncMock(
                side_effect=lambda session, key: SimpleNamespace(value=settings_map.get(key)))), \
             patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                 return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.publish_job', new=AsyncMock()):
            await self.click('infra:bridge_remove:8')
            warn = self.message.edit_text.await_args.args[0]
            self.assertIn('cdn.google.com', warn)
            await self.click('infra:bridge_remove_confirm:8')
        kind = self.service.enqueue.await_args.args[3]
        self.assertEqual(kind, 'bridge_delete')

    async def test_bridge_routes_flow_validates_and_saves_extras(self):
        self.message.text = 'dl.google.com 127.0.0.1:8443\nfonts.google.com 127.0.0.1:8443'
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.save_config', new=AsyncMock()) as saved:
            await self.click('infra:bridge_routes')
            self.assertEqual(await self.state.get_state(), InfrastructureStates.input.state)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        key, value = saved.call_args.args[1], saved.call_args.args[2]
        self.assertEqual(key, 'bridge_extra_routes')
        self.assertEqual(value, ['dl.google.com 127.0.0.1:8443', 'fonts.google.com 127.0.0.1:8443'])

    async def test_bridge_routes_flow_rejects_malformed_lines(self):
        self.message.text = 'dl.google.com'
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.save_config', new=AsyncMock()) as saved:
            await self.click('infra:bridge_routes')
            await self.state.set_state(InfrastructureStates.input)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        # The error is answered to the admin, nothing is saved.
        saved.assert_not_awaited()
        self.assertIn('Invalid route', self.message.answer.await_args.args[0])

    async def test_bridge_apply_looks_up_syncs_repoints_and_renders(self):
        routes = [{'sni': 'dl.google.com', 'backend': '127.0.0.1:8443'}]
        ensure = AsyncMock(return_value=('profile', 'in-wildcard'))
        sync = AsyncMock(return_value=False)
        repoint = AsyncMock(return_value=1)
        rendered = AsyncMock()
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.desired_routes', new=AsyncMock(return_value=routes)), \
             patch('bot.handlers.admin.infrastructure.ensure_bridge_wildcard', new=ensure), \
             patch('bot.handlers.admin.infrastructure.sync_wildcard_snis', new=sync), \
             patch('bot.handlers.admin.infrastructure.repoint_bridge_hosts', new=repoint), \
             patch('bot.handlers.admin.infrastructure.ansible_bridge', new=rendered):
            await self.click('infra:bridge_apply')
        ensure.assert_awaited_once()
        sync.assert_awaited_once_with(self.service.api, 'profile', ['dl.google.com'])
        repoint.assert_awaited_once()
        rendered.assert_awaited_once()
        self.assertEqual(rendered.await_args.args[2], routes)
        self.assertIn('1', self.message.answer.await_args.args[0])

    async def test_bridge_address_input_does_not_repoint_before_verification(self):
        repoint = AsyncMock(return_value=2)
        self.message.text = 'bridge.example.net'
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.save_config', new=AsyncMock()), \
             patch('bot.handlers.admin.infrastructure.repoint_bridge_hosts', new=repoint):
            await self.click('infra:bridge_setting:bridge_address')
            await self.state.set_state(InfrastructureStates.input)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        # Stale hosts follow the address only after bridge_test pins the key.
        repoint.assert_not_awaited()

    async def test_bridge_test_pins_host_key_and_repoints(self):
        self.service.settings = SimpleNamespace(INFRA_SSH_PRIVATE_KEY='/tmp/key')
        repoint = AsyncMock(return_value=1)
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.save_config', new=AsyncMock()), \
             patch('bot.handlers.admin.infrastructure.host_key', new=AsyncMock(
                 return_value=('ssh-ed25519 KEY', 'AA:BB'))), \
             patch('bot.handlers.admin.infrastructure.asyncssh.connect', new=MagicMock()), \
             patch('bot.handlers.admin.infrastructure.asyncssh.import_public_key',
                 new=Mock(return_value=object())), \
             patch('bot.handlers.admin.infrastructure.repoint_bridge_hosts', new=repoint):
            await self.click('infra:bridge_test')
        repoint.assert_awaited_once()
        answers = ' '.join(call.args[0] for call in self.message.answer.await_args_list)
        self.assertIn('AA:BB', answers)
        self.assertIn('1', answers)

    async def test_bridge_wildcard_flow_changes_domain(self):
        changed = AsyncMock(return_value=2)
        self.message.text = 'cloudflare.com'
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.change_wildcard_dest', new=changed):
            await self.click('infra:bridge_wildcard')
            self.assertEqual(await self.state.get_state(), InfrastructureStates.input.state)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        changed.assert_awaited_once()
        self.assertEqual(changed.await_args.args[2], 'cloudflare.com')
        answers = ' '.join(call.args[0] for call in self.message.answer.await_args_list)
        self.assertIn('cloudflare.com', answers)

    async def test_bridge_wildcard_backend_setting_is_saved(self):
        self.message.text = '10.0.0.5:8443'
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(
                return_value=dict(BRIDGE_CONFIG))), \
             patch('bot.handlers.admin.infrastructure.save_config', new=AsyncMock()) as saved:
            await self.click('infra:bridge_setting:bridge_wildcard_backend')
            await self.state.set_state(InfrastructureStates.input)
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        key, value = saved.call_args.args[1], saved.call_args.args[2]
        self.assertEqual(key, 'bridge_wildcard_backend')
        self.assertEqual(value, '10.0.0.5:8443')


class BridgePlaybookTests(unittest.TestCase):
    PLAYBOOK = Path(__file__).resolve().parents[1] / 'infra' / 'ansible' / 'bridge.yml'

    def load_tasks(self):
        import yaml
        document = yaml.safe_load(self.PLAYBOOK.read_text())
        return {task['name']: task for play in document for task in play['tasks']}

    def rendered_template(self):
        return self.load_tasks()['Render the haproxy configuration']['ansible.builtin.copy']['content']

    def test_playbook_passes_ansible_syntax_check(self):
        import subprocess, sys, tempfile
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / 'inventory'
            inventory.write_text('localhost ansible_connection=local\n')
            result = subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '--syntax-check',
                '-i', str(inventory), str(self.PLAYBOOK)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_playbook_defaults_bridge_backends_for_empty_tables(self):
        # With no routes the set_fact loop never runs; without this default the
        # copy task fails on an undefined fact and removal of the last bridge
        # would hang forever.
        import yaml
        document = yaml.safe_load(self.PLAYBOOK.read_text())
        self.assertEqual(document[0]['vars']['bridge_backends'], [])

    def test_rendered_configuration_routes_every_sni_to_its_backend(self):
        from jinja2 import Environment, StrictUndefined
        routes = [
            {'sni': 'dl.google.com', 'backend': '127.0.0.1:8443'},
            {'sni': 'cdn.google.com', 'backend': '[2001:db8::1]:443'},
        ]
        content = self.rendered_template()
        rendered = Environment(undefined=StrictUndefined).from_string(content).render(
            bridge_routes=routes, bridge_backends=[routes[0]['backend'], routes[1]['backend']])
        self.assertIn('acl sni_1 req.ssl_sni -i dl.google.com', rendered)
        self.assertIn('use_backend 127_0_0_1_8443 if sni_1', rendered)
        self.assertIn('acl sni_2 req.ssl_sni -i cdn.google.com', rendered)
        self.assertIn('use_backend 2001_db8__1_443 if sni_2', rendered)
        self.assertIn('backend 127_0_0_1_8443\n    mode tcp\n    server target 127.0.0.1:8443', rendered)
        self.assertIn('server target [2001:db8::1]:443', rendered)
        self.assertIn('default_backend reject', rendered)
        self.assertIn('backend reject', rendered)
        self.assertNotIn('{%', rendered)
        self.assertNotIn('{{', rendered)

    def test_rendered_configuration_rejects_without_routes(self):
        from jinja2 import Environment, StrictUndefined
        content = self.rendered_template()
        rendered = Environment(undefined=StrictUndefined).from_string(content).render(
            bridge_routes=[], bridge_backends=[])
        self.assertIn('default_backend reject', rendered)
        self.assertNotIn('use_backend', rendered)
        self.assertNotIn('server target', rendered.replace('backend reject', ''))


class BridgeLabelTests(unittest.TestCase):
    def test_operation_and_stage_labels_exist_in_both_locales(self):
        from bot.services.infrastructure.job_messages import kind_label, stage_label
        for language in ('en', 'ru'):
            catalog = JsonI18n('locales', default=language)
            translate = lambda key, **values: catalog.gettext(language, key, **values)
            for kind in ('bridge_create', 'bridge_delete'):
                job = SimpleNamespace(kind=kind, status='running', stage='bridge_profile')
                self.assertNotEqual(kind_label(job, translate), 'infra_operation_' + kind)
                self.assertFalse(translate('infra_result_' + kind).startswith('infra_'))
            for stage in ('bridge_profile', 'bridge', 'bridge_host'):
                label = stage_label(SimpleNamespace(stage=stage, status='running'), translate)
                self.assertFalse(label.startswith('infra_'), label)
