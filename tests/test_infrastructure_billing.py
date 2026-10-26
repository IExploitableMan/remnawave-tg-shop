import copy
import json
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.services.infrastructure.billing import reconcile_billing
from db.models import InfrastructureJob as Job


class BillingApi:
    def __init__(self):
        self.entries = [{'uuid': 'old', 'nodeUuid': 'panel-node', 'providerUuid': 'old-provider',
                         'name': 'node1', 'nextBillingAt': '2026-11-01T00:00:00Z'}]
        self.history = [{'providerUuid': 'old-provider', 'amount': 100}]
        self.calls = []
        self.fail_create = None
        self.fail_delete = False

    async def billing(self):
        return {'billingNodes': copy.deepcopy(self.entries)}

    async def call(self, method, path, payload=None):
        self.calls.append((method, path))
        if method == 'POST':
            if self.fail_create == 'before':
                raise RuntimeError('create failed')
            self.entries.append(dict(payload, uuid='new'))
            if self.fail_create == 'after':
                raise RuntimeError('response lost')
            return self.entries[-1]
        if method == 'PATCH':
            for entry in self.entries:
                if entry['uuid'] in payload['uuids']:
                    entry['nextBillingAt'] = payload['nextBillingAt']
            return {}
        if method == 'DELETE':
            if self.fail_delete:
                raise RuntimeError('delete failed')
            self.entries = [e for e in self.entries if e['uuid'] != path.rsplit('/', 1)[1]]
            return {}
        raise AssertionError((method, path))


class BillingProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = BillingApi()
        self.row = SimpleNamespace(billing_uuid='old')
        self.job = SimpleNamespace(payload=json.dumps({'provider_uuid': 'new-provider'}))
        outer = self
        class Session:
            async def get(self, model, identifier):
                return outer.job if model is Job else outer.row
            async def commit(self):
                pass
        @asynccontextmanager
        async def factory():
            yield Session()
        self.service = SimpleNamespace(api=self.api, factory=factory)
        self.node = SimpleNamespace(id=1, panel_uuid='panel-node', billing_uuid='old',
                                    provider_uuid='new-provider', name='node1')
        self.deployment = SimpleNamespace(paid_until=datetime(2026, 12, 1, tzinfo=timezone.utc))

    async def reconcile(self):
        return await reconcile_billing(self.service, self.node, self.deployment, 1)

    async def test_new_assignment_is_confirmed_before_old_assignment_removed(self):
        history = copy.deepcopy(self.api.history)
        await self.reconcile()
        self.assertLess(self.api.calls.index(('POST', '/infra-billing/nodes')),
                        self.api.calls.index(('DELETE', '/infra-billing/nodes/old')))
        self.assertEqual(self.api.history, history)
        self.assertEqual(self.row.billing_uuid, 'new')
        self.assertEqual([e['providerUuid'] for e in self.api.entries], ['new-provider'])
        self.assertEqual(json.loads(self.job.payload)['billing_previous_uuid'], 'old')

    async def test_failed_creation_keeps_original_assignment(self):
        self.api.fail_create = 'before'
        with self.assertRaisesRegex(RuntimeError, 'create failed'):
            await self.reconcile()
        self.assertEqual([e['uuid'] for e in self.api.entries], ['old'])
        self.assertFalse(any(method == 'DELETE' for method, path in self.api.calls))
        self.assertEqual(self.row.billing_uuid, 'old')

    async def test_lost_create_response_does_not_duplicate_or_discard_assignment(self):
        self.api.fail_create = 'after'
        await self.reconcile()
        self.assertEqual([e['uuid'] for e in self.api.entries], ['new'])
        self.assertEqual(self.row.billing_uuid, 'new')

    async def test_cleanup_failure_can_be_retried_without_duplicate_creation(self):
        self.api.fail_delete = True
        with self.assertRaisesRegex(RuntimeError, 'старую привязку'):
            await self.reconcile()
        self.assertEqual(self.row.billing_uuid, 'new')
        self.api.fail_delete = False
        await self.reconcile()
        self.assertEqual([e['uuid'] for e in self.api.entries], ['new'])
        self.assertEqual(sum(method == 'POST' for method, path in self.api.calls), 1)

    async def test_reassigned_previous_record_is_not_deleted(self):
        self.api.entries[0]['nodeUuid'] = 'other-node'
        self.job.payload = json.dumps({'provider_uuid': 'new-provider', 'billing_previous_uuid': 'old'})
        await self.reconcile()
        self.assertIn('old', [e['uuid'] for e in self.api.entries])
        self.assertFalse(any(method == 'DELETE' for method, path in self.api.calls))
