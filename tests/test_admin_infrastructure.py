"""Regression coverage for the real infrastructure handlers and wizard state."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from cryptography.fernet import Fernet

from bot.handlers.admin.infrastructure import (
    InfrastructureStates, infrastructure_callback, infrastructure_input, ask, confirmation,
)
from bot.middlewares.i18n import JsonI18n
from bot.services.infrastructure.diagnostics import send_artifacts


class InfrastructureHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=111, user_id=111))
        self.settings = SimpleNamespace(ADMIN_IDS=[111], INFRA_ENABLED=True, DEFAULT_LANGUAGE='en')
        self.catalog = JsonI18n('locales', default='en')
        self.i18n = {'current_language': 'en', 'i18n_instance': self.catalog}
        self.session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), get=AsyncMock(return_value=None))
        key = Fernet.generate_key()
        self.service = SimpleNamespace(cipher=lambda: Fernet(key),
            api=SimpleNamespace(providers=AsyncMock(return_value=[{'uuid': 'provider', 'name': 'Existing'}]),
                                call=AsyncMock(return_value={'uuid': 'new-provider'})))
        self.message = SimpleNamespace(from_user=SimpleNamespace(id=111), chat=SimpleNamespace(type='private'),
            answer=AsyncMock(), edit_text=AsyncMock(), text='Provider\nhttps://example.com')
        self.callback = SimpleNamespace(from_user=self.message.from_user, message=self.message, answer=AsyncMock())

    async def asyncTearDown(self):
        await self.storage.close()

    async def wizard(self, **changes):
        data = {'flow': 'wizard', 'phase': 'providers', 'position': 7,
            'steps': ['address', 'ssh_port', 'ssh_user', 'password', 'host_address', 'country', 'days'],
            'address': '203.0.113.1', 'host_address': 'vpn.example.com', 'ssh_port': 22, 'ssh_user': 'root',
            'password': self.service.cipher().encrypt(b'secret').decode(), 'country': 'DE', 'days': 15}
        data.update(changes)
        await self.state.set_data(data)
        await self.state.set_state(InfrastructureStates.input)
        return data

    async def click(self, payload):
        self.callback.data = payload
        await infrastructure_callback(self.callback, self.state, self.session, self.service, self.settings, self.i18n)

    async def test_existing_provider_timeout_is_visible_and_wizard_is_preserved(self):
        original = await self.wizard()
        with patch('bot.handlers.admin.infrastructure.host_key', new=AsyncMock(side_effect=TimeoutError())):
            await self.click('infra:pickprovider:provider')
        data = await self.state.get_data()
        self.assertEqual(data['provider_uuid'], 'provider')
        self.assertEqual(data['password'], original['password'])
        texts = [call.args[0] for call in self.message.answer.await_args_list]
        self.assertIn('Checking SSH', texts[0])
        self.assertIn('did not respond', texts[-1])
        self.assertNotIn('secret', '\n'.join(texts))
        self.assertEqual(self.message.answer.await_args.kwargs['reply_markup'].inline_keyboard[-1][0].callback_data, 'infra:wizard_back')

    async def test_provider_creation_selects_new_provider_even_after_previous_selection(self):
        await self.wizard(provider_uuid='old-provider')
        await self.click('infra:addprovider')
        self.assertEqual((await self.state.get_data())['flow'], 'addprovider')
        with patch('bot.handlers.admin.infrastructure.host_key', new=AsyncMock(return_value=('public-key', 'fingerprint'))):
            await infrastructure_input(self.message, self.state, self.session, self.service, self.settings, self.i18n)
        data = await self.state.get_data()
        self.assertEqual(data['provider_uuid'], 'new-provider')
        self.assertEqual(data['days'], 15)
        self.assertEqual(await self.state.get_state(), InfrastructureStates.confirm.state)
        self.assertIn('Node installation', self.message.answer.await_args.args[0])

    async def test_back_from_provider_creation_returns_to_wizard_not_home(self):
        original = await self.wizard()
        await self.click('infra:addprovider')
        markup = self.message.answer.await_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, 'infra:wizard_provider_back')
        await self.click('infra:wizard_provider_back')
        data = await self.state.get_data()
        self.assertEqual(data['flow'], 'wizard')
        self.assertEqual(data['password'], original['password'])
        self.assertIn('Choose a provider', self.message.answer.await_args.args[0])
        await self.click('infra:wizard_back')
        self.assertEqual((await self.state.get_data())['position'], 6)
        self.assertIn('how many days', self.message.answer.await_args.args[0])

    async def test_back_from_confirmation_returns_to_provider_and_keeps_inputs(self):
        await self.wizard(phase='confirm')
        await self.state.set_state(InfrastructureStates.confirm)
        await self.click('infra:wizard_back')
        self.assertEqual((await self.state.get_data())['phase'], 'providers')
        self.assertEqual((await self.state.get_data())['host_address'], 'vpn.example.com')
        self.assertEqual(await self.state.get_state(), InfrastructureStates.input.state)

    async def test_host_prompt_explains_client_address_and_default(self):
        await self.wizard(phase='fields', position=4)
        from bot.services.infrastructure.localization import bind_language
        bind_language(self.i18n, self.settings, self.callback)
        await ask(self.message, self.state, self.service, self.session)
        text = self.message.answer.await_args.args[0]
        self.assertIn('VPN clients', text)
        self.assertIn('subscription', text)
        buttons = self.message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertTrue(any('203.0.113.1' in row[0].text for row in buttons))
        self.assertEqual(buttons[-1][0].text, '⬅️ Back')

    async def test_home_localization_and_bottom_back_for_both_languages(self):
        for language, label in [('en', '🖥 Nodes'), ('ru', '🖥 Ноды')]:
            self.i18n['current_language'] = language
            await self.click('infra:home')
            markup = self.message.edit_text.await_args.kwargs['reply_markup']
            self.assertEqual(markup.inline_keyboard[0][0].text, label)
            self.assertEqual(markup.inline_keyboard[-1][0].callback_data, 'admin_action:runtime_settings')

    async def test_input_back_clears_form_and_returns_to_parent(self):
        await self.state.set_data({'flow': 'setting', 'key': 'default_days', 'return_to': 'infra:settings'})
        await self.state.set_state(InfrastructureStates.input)
        with patch('bot.handlers.admin.infrastructure.read_config', new=AsyncMock(return_value={
            'default_days': 30, 'warning_hours': [24], 'expire_exclude_tg': False, 'panel_ip': '203.0.113.2',
            'large_repeats': 1, 'node_version_2': '2.8.0', 'node_version_3': '3.4.2'})):
            await self.click('infra:settings')
        self.assertIsNone(await self.state.get_state())
        text = self.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text
        self.assertIn('Initial duration', text)
        self.assertNotIn('infra_', text)

    async def test_short_report_is_readable_html_message_not_document(self):
        service = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock()),
            settings=self.settings, factory=None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath('results.json').write_text(json.dumps({'node': 'node1 <DE>', 'address': '203.0.113.1',
                'results': [{'status': 'ok', 'output': 'all checks passed'}]}))
            await send_artifacts(service, 111, root, [], 'small')
            service.bot.send_document.assert_not_awaited()
            text = service.bot.send_message.await_args.args[1]
            self.assertIn('<b>⚡ Quick node check</b>', text)
            self.assertIn('node1 &lt;DE&gt;', text)
            self.assertIn('✅ Node is connected', text)
            root.joinpath('results.json').write_text(json.dumps({'node': 'node1', 'address': '203.0.113.1',
                'results': [{'status': 'failed', 'output': 'connection <failed>'}]}))
            await send_artifacts(service, 111, root, [], 'small')
            self.assertIn('connection &lt;failed&gt;', service.bot.send_message.await_args.args[1])

    async def test_saved_large_report_rerenders_without_contacting_server(self):
        from db.models import InfrastructureReport as Report, InfrastructureDeployment as Deployment
        with tempfile.TemporaryDirectory() as temporary:
            self.settings.INFRA_ARTIFACT_DIR = temporary
            root = Path(temporary) / 'reports' / '19'
            root.mkdir(parents=True)
            root.joinpath('results.json').write_text(json.dumps({'node': 'node1', 'address': '203.0.113.1',
                'created_at': '2026-10-13', 'level': 'large', 'results': [
                    {'id': 'bench', 'title': 'System', 'round': 1, 'status': 'partial',
                     'output': 'CPU: EPYC\nKazan  Download failed', 'metrics': {}}]}))
            report = SimpleNamespace(id=19, deployment_id=1, level='large',
                manifest=json.dumps({'directory': '19', 'files': ['old.pdf']}))
            deployment = SimpleNamespace(node_id=1)
            self.session.get.side_effect = lambda model, identifier: report if model is Report else deployment
            self.service.node = AsyncMock(return_value=SimpleNamespace(name='node1'))
            self.service.bot = SimpleNamespace(send_document=AsyncMock())
            with patch('bot.handlers.admin.infrastructure.host_key', new=AsyncMock()) as ssh:
                await self.click('infra:report:19')
            ssh.assert_not_awaited()
            self.service.api.call.assert_not_awaited()
            self.assertEqual(json.loads(report.manifest)['renderer_version'], 2)
            self.assertTrue((root/'report.pdf').exists())
            self.assertEqual(self.service.bot.send_document.await_args.args[1].path, root/'report.pdf')
            self.assertEqual(self.message.answer.await_args.kwargs['reply_markup'].inline_keyboard[-1][0].callback_data, 'infra:reports:1')
