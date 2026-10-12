import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bot.services.panel_api_service import PanelApiService


class PanelV3Tests(unittest.IsolatedAsyncioTestCase):
    def service(self, major='3'):
        service = PanelApiService(SimpleNamespace(PANEL_API_URL='https://panel/api',
            PANEL_API_KEY='test', PANEL_API_VERSION=major, USER_HWID_DEVICE_LIMIT=2))
        service._request = AsyncMock()
        return service

    async def test_metadata_detection_is_cached_and_offline_never_guesses(self):
        service = self.service('auto')
        service._request.return_value = {'error': True}
        with self.assertRaises(RuntimeError):
            await service.api_major()
        service._request.return_value = {'response': {'version': '3.4.5'}}
        self.assertEqual(await service.api_major(), 3)
        self.assertEqual(await service.api_major(), 3)
        self.assertEqual(service._request.await_count, 2)

    async def test_legacy_identifier_is_not_sent_to_v3(self):
        service = self.service()
        user = await service.normalize_user({'id': 42, 'username': 'tg_99'})
        service._request.return_value = {'response': {'id': 42, 'status': 'ACTIVE'}}
        payload = {'uuid': user['uuid'], 'expireAt': '2030-01-01T00:00:00Z'}
        await service.update_user_details_on_panel(user['uuid'], payload)
        call = service._request.call_args
        self.assertEqual(call.kwargs['json']['id'], 42)
        self.assertNotIn('uuid', call.kwargs['json'])
        self.assertIn('uuid', payload)  # caller's dictionary is never mutated

    async def test_unknown_reference_does_not_attempt_mutation(self):
        service = self.service()
        with self.assertRaises(ValueError):
            await service.reset_user_traffic('unknown-legacy-uuid')
        service._request.assert_not_called()

    async def test_hwid_uses_numeric_id_and_empty_success(self):
        service = self.service()
        user = await service.normalize_user({'id': 42})
        service._request.return_value = {'status': 'success', 'code': 204}
        self.assertTrue(await service.disconnect_device(user['uuid'], 'device'))
        self.assertEqual(service._request.call_args.kwargs['json'], {'userId': 42, 'hwid': 'device'})

    async def test_v2_preserves_uuid_request(self):
        service = self.service('2')
        service._request.return_value = {'response': {'uuid': 'legacy', 'id': 42}}
        await service.update_user_details_on_panel('legacy', {'status': 'ACTIVE'})
        self.assertEqual(service._request.call_args.kwargs['json']['uuid'], 'legacy')

    async def test_filter_ignoring_panel_cannot_leak_another_account(self):
        service = self.service()
        service._request.return_value = {'response': {'users': [
            {'id': 1, 'telegramId': 100}, {'id': 2, 'telegramId': 200}]}}
        users = await service.get_users_by_filter(telegram_id=100)
        self.assertEqual([u['id'] for u in users], [1])
        import json
        self.assertEqual(json.loads(service._request.call_args.kwargs['params']['filterModes'])['telegramId'], 'equals')

    async def test_panel_scope_changes_local_reference(self):
        first = self.service()
        second = PanelApiService(SimpleNamespace(PANEL_API_URL='https://other/api', PANEL_API_KEY='test'))
        a = await first.normalize_user({'id': 42})
        b = await second.normalize_user({'id': 42})
        self.assertNotEqual(a['uuid'], b['uuid'])
