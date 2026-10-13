import os
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.exc import IntegrityError
from db.models import (User, PanelUserIdentity, InfrastructureNode as Node,
                       InfrastructureDeployment as Deployment, InfrastructureJob as Job)
from bot.services.panel_identity_service import PanelIdentityService
from bot.services.infrastructure.service import InfrastructureService


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Dedicated TEST_DATABASE_URL is required')
class InfrastructureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedded_migrations_preserve_application_logging(self):
        import logging
        from alembic.config import Config
        from db.alembic_runner import _run_upgrade
        root = logging.getLogger()
        dispatcher = logging.getLogger('aiogram.dispatcher')
        before = root.level, dispatcher.disabled
        root.setLevel(logging.INFO)
        dispatcher.disabled = False
        try:
            async with self.engine.begin() as connection:
                await connection.run_sync(_run_upgrade, Config('alembic.ini'))
            self.assertEqual(root.level, logging.INFO)
            self.assertFalse(dispatcher.disabled)
        finally:
            root.setLevel(before[0])
            dispatcher.disabled = before[1]

    @classmethod
    def setUpClass(cls):
        import subprocess, sys
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], check=True, capture_output=True)

    async def asyncSetUp(self):
        self.engine = create_async_engine(os.environ['TEST_DATABASE_URL'])
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.identity = PanelIdentityService(f'https://test-{uuid4()}/api', self.factory)
        self.settings = SimpleNamespace(INFRA_ENCRYPTION_KEY=Fernet.generate_key().decode(), ADMIN_IDS=[111, 222])
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                                  edit_message_text=AsyncMock())
        self.service = InfrastructureService(self.settings, SimpleNamespace(identities=self.identity), self.bot, self.factory)
        self.user_id = -int(uuid4().int % 1000000000) - 1000
        async with self.factory() as session:
            session.add(User(user_id=self.user_id, panel_user_uuid=str(uuid4()), addon_panel_user_uuid=str(uuid4())))
            await session.commit()

    async def asyncTearDown(self):
        async with self.factory() as session:
            nodes = (await session.execute(select(Node.id).where(Node.panel_scope == self.identity.scope))).scalars().all()
            await session.execute(delete(Job).where(Job.node_id.in_(nodes)))
            await session.execute(delete(Deployment).where(Deployment.node_id.in_(nodes)))
            await session.execute(delete(Node).where(Node.id.in_(nodes)))
            await session.execute(delete(PanelUserIdentity).where(PanelUserIdentity.panel_scope == self.identity.scope))
            await session.execute(delete(User).where(User.user_id == self.user_id))
            from db.models import AppSetting
            await session.execute(delete(AppSetting).where(
                (AppSetting.key.like(f'%{self.identity.scope}')) |
                (AppSetting.key.like(f'infra.keys.{self.identity.scope}.%'))))
            await session.commit()
        await self.engine.dispose()

    async def node_fixture(self):
        async with self.factory() as session:
            node = Node(panel_scope=self.identity.scope, name='node100 (RU)', country='RU', provider_uuid=str(uuid4()))
            session.add(node); await session.flush()
            deployment = Deployment(node_id=node.id, state='active', address='203.0.113.1', host_address='203.0.113.1',
                ssh_port=22, ssh_user='root', credentials=self.service.cipher().encrypt(b'root-password').decode(),
                profile_uuid=str(uuid4()), inbound_uuid=str(uuid4()), node_version='3.4.2',
                paid_until=datetime.now(timezone.utc) + timedelta(days=30))
            session.add(deployment); await session.commit()
            return node, deployment

    async def test_identity_survives_restart_and_preserves_legacy_base_addon_links(self):
        async with self.factory() as session:
            user = await session.get(User, self.user_id)
            base_ref, addon_ref = user.panel_user_uuid, user.addon_panel_user_uuid
        base = await self.identity.normalize({'id': 42, 'telegramId': self.user_id, 'username': 'custom_name'})
        # A generated addon name establishes account identity without telegramId.
        addon = await self.identity.normalize({'id': 43, 'username': f'web_{abs(self.user_id)}_addon'})
        self.assertEqual(base['uuid'], base_ref)
        self.assertEqual(addon['uuid'], addon_ref)
        restarted = PanelIdentityService(f'https://unused/api', self.factory)
        restarted.scope = self.identity.scope
        self.assertEqual(await restarted.resolve(base_ref), 42)
        self.assertEqual(await restarted.resolve(addon_ref), 43)

    async def test_database_allows_queue_but_prevents_parallel_running_jobs(self):
        node, deployment = await self.node_fixture()
        deployment.state = 'replaced'
        async with self.factory() as session:
            with self.assertRaises(ValueError):
                await self.service.enqueue(session, node, deployment, 'provision', 111)
        deployment.state = 'active'
        async with self.factory() as session:
            first = await self.service.enqueue(session, node, deployment, 'small', 111)
            first.status = 'running'
            await session.commit()
        async with self.factory() as session:
            second = await self.service.enqueue(session, node, deployment, 'large', 111)
            third = await self.service.enqueue(session, node, deployment, 'medium', 222)
            await session.commit()
            self.assertEqual((second.status, third.status), ('pending', 'pending'))
        async with self.factory() as session:
            session.add(Job(node_id=node.id, deployment_id=deployment.id, kind='small', requested_by=222, status='running'))
            with self.assertRaises(IntegrityError):
                await session.commit()
            await session.rollback()
        async with self.factory() as session:
            with self.assertRaises(ValueError):
                await self.service.enqueue(session, node, deployment, 'provision', 111)
        async with self.factory() as session:
            (await session.get(Job, first.id)).status = 'done'
            await session.flush()
            (await session.get(Job, second.id)).status = 'running'
            await session.commit()
            queued = (await session.execute(select(Job).where(Job.node_id == node.id).order_by(Job.id))).scalars().all()
            self.assertEqual([j.status for j in queued], ['done', 'running', 'pending'])

    async def test_key_propagation_waits_for_busy_node_and_survives_service_restart(self):
        node, deployment = await self.node_fixture()
        async with self.factory() as session:
            job = await self.service.enqueue(session, node, deployment, 'medium', 111)
            await self.service.schedule_key_updates(session, 222)
            await session.commit()
        await self.service.enqueue_pending_keys()
        async with self.factory() as session:
            self.assertEqual(len((await session.execute(select(Job).where(Job.node_id == node.id))).scalars().all()), 1)
            (await session.get(Job, job.id)).status = 'done'
            await session.commit()
        restarted = InfrastructureService(self.settings, SimpleNamespace(identities=self.identity), self.bot, self.factory)
        await restarted.enqueue_pending_keys()
        await restarted.enqueue_pending_keys()
        async with self.factory() as session:
            jobs = (await session.execute(select(Job).where(Job.node_id == node.id).order_by(Job.id))).scalars().all()
            self.assertEqual([(j.kind, j.requested_by) for j in jobs], [('medium', 111), ('keys', 222)])

    async def test_archived_node_is_readable_for_history_but_not_operations(self):
        node, deployment = await self.node_fixture()
        async with self.factory() as session:
            (await session.get(Node, node.id)).deleted = True
            await session.commit()
            with self.assertRaises(ValueError):
                await self.service.node(session, node.id)
            archived = await self.service.node(session, node.id, include_deleted=True)
            self.assertEqual(archived.id, node.id)
            foreign = InfrastructureService(self.settings, SimpleNamespace(identities=SimpleNamespace(scope='other-panel')),
                                             self.bot, self.factory)
            with self.assertRaises(ValueError):
                await foreign.node(session, node.id, include_deleted=True)

    async def test_credentials_without_billing_do_not_invent_a_deadline(self):
        node, deployment = await self.node_fixture()
        async with self.factory() as session:
            (await session.get(Deployment, deployment.id)).paid_until = None
            await session.commit()
        async with self.factory() as session:
            stored = await self.service.deployment(session, node.id)
            self.assertIsNone(stored.paid_until)
            snapshot = await self.service.snapshot(session, node.id)
            self.assertIn('root-password', snapshot[0])

    async def test_failed_upgrade_does_not_report_uninstalled_version(self):
        from unittest.mock import patch
        from bot.services.infrastructure.provisioning import run_job
        node, deployment = await self.node_fixture()
        self.service.panel.api_major = AsyncMock(return_value=3)
        self.service.api.node_secret = AsyncMock(return_value='test-secret')
        self.service.validate_deployment = AsyncMock()
        self.service.api.nodes = AsyncMock(return_value=[{'uuid': node.panel_uuid, 'versions': {'node': '2.8.0'}}])
        async with self.factory() as session:
            (await session.get(Deployment, deployment.id)).node_version = '2.8.0'
            job = await self.service.enqueue(session, node, deployment, 'upgrade', 111)
            await session.commit()
        with patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock(side_effect=RuntimeError('Installation failed'))) as install, \
             patch('bot.services.infrastructure.versions.latest_node_version', new=AsyncMock(return_value='3.4.2')):
            await run_job(self.service, job.id)
        install.assert_awaited_once()
        async with self.factory() as session:
            self.assertEqual((await session.get(Deployment, deployment.id)).node_version, '2.8.0')
            self.assertEqual((await session.get(Job, job.id)).status, 'failed')

    async def test_shutdown_preserves_job_completed_during_worker_poll(self):
        import asyncio
        from unittest.mock import patch
        node, deployment = await self.node_fixture()
        async with self.factory() as session:
            job = await self.service.enqueue(session, node, deployment, 'small', 111)
            await session.commit()
        completed, billing_started = asyncio.Event(), asyncio.Event()

        async def finish_job(service, job_id):
            async with self.factory() as session:
                row = await session.get(Job, job_id)
                row.status, row.result = 'done', 'Готово'
                row.finished_at = datetime.now(timezone.utc)
                await session.commit()
            completed.set()

        async def billing_check():
            await completed.wait()
            billing_started.set()
            await asyncio.Event().wait()

        self.service.api.call = AsyncMock(return_value={})
        self.service.enqueue_pending_keys = AsyncMock()
        self.service.check_billing = billing_check
        with patch('bot.services.infrastructure.provisioning.run_job', new=finish_job):
            worker = asyncio.create_task(self.service.worker())
            try:
                await asyncio.wait_for(billing_started.wait(), 5)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        async with self.factory() as session:
            row = await session.get(Job, job.id)
            self.assertEqual(row.status, 'done')
            self.assertIsNotNone(row.finished_at)

    async def test_snapshot_updates_all_admins_independent_of_opt_out(self):
        await self.node_fixture()
        from db.dal import app_settings_dal
        async with self.factory() as session:
            await app_settings_dal.upsert_setting(session, 'infra.notify.222', 'false')
            await session.commit()
        await self.service.publish_snapshots()
        self.assertEqual({call.args[0] for call in self.bot.send_message.call_args_list}, {111, 222})
        self.assertIn('<code>root-password</code>', self.bot.send_message.call_args.args[1])
        self.bot.send_message.reset_mock()
        await self.service.publish_snapshots()
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        self.bot.send_message.assert_not_called()

    async def test_save_existing_access_ignores_multiple_hosts_inbounds_and_never_patches_panel(self):
        import asyncssh
        from unittest.mock import patch, MagicMock
        node, old = await self.node_fixture()
        async with self.factory() as session:
            await session.execute(delete(Deployment).where(Deployment.id == old.id))
            actual = await session.get(Node, node.id)
            actual.panel_uuid = str(uuid4())
            await session.commit()
        public = asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode().strip()
        self.service.api.nodes = AsyncMock(return_value=[{'uuid': actual.panel_uuid, 'name': 'existing',
            'countryCode': 'DE', 'address': 'vpn.example.com', 'configProfile': {
                'activeConfigProfileUuid': 'profile', 'activeInbounds': ['one', 'two']}}])
        self.service.api.billing = AsyncMock(return_value={'billingNodes': []})
        self.service.api.call = AsyncMock()
        self.service.panel.get_all_hosts = AsyncMock(return_value=[{'uuid': 'one', 'nodes': [actual.panel_uuid]},
            {'uuid': 'two', 'nodes': [actual.panel_uuid]}])
        connect = MagicMock()
        connect.return_value.__aenter__ = AsyncMock()
        connect.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch('bot.services.infrastructure.provisioning.host_key', new=AsyncMock(return_value=(public, 'fingerprint'))), \
                patch('asyncssh.connect', connect):
            async with self.factory() as session:
                node = await session.get(Node, node.id)
                dep, fingerprint = await self.service.save_access(session, node, '203.0.113.1', 22, 'root', 'password')
                await session.commit()
                self.assertIsNone(dep.inbound_uuid)
                self.assertIsNone(node.host_uuid)
                self.assertEqual(dep.panel_address, 'vpn.example.com')
                self.assertEqual(dep.host_address, 'vpn.example.com')
                self.assertEqual(self.service.cipher().decrypt(dep.credentials.encode()).decode(), 'password')
                await self.service.validate_deployment(session, node, dep)
        self.service.api.call.assert_not_awaited()

    async def test_imported_node_deadline_never_changes_host_automatically(self):
        from unittest.mock import patch
        node, deployment = await self.node_fixture()
        panel_uuid, host_uuid = str(uuid4()), str(uuid4())
        async with self.factory() as session:
            stored = await session.get(Node, node.id)
            stored.panel_uuid, stored.host_uuid, stored.host_automation = panel_uuid, host_uuid, False
            await session.commit()
        self.service.api.billing = AsyncMock(return_value={'billingNodes': [{'uuid': str(uuid4()),
            'nodeUuid': panel_uuid, 'providerUuid': node.provider_uuid,
            'nextBillingAt': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}]})
        self.service.api.providers = AsyncMock(return_value=[])
        self.service.api.call = AsyncMock()
        self.service.panel.get_all_hosts = AsyncMock()
        with patch('bot.services.infrastructure.service.read_config', new=AsyncMock(return_value={
                'warning_hours': [], 'expire_exclude_tg': True, 'tg_squad_uuid': 'tg'})):
            await self.service.check_billing()
        self.service.api.call.assert_not_awaited()
        self.service.panel.get_all_hosts.assert_not_awaited()

    async def test_operation_progress_survives_restart_and_completion_is_separate(self):
        from bot.services.infrastructure.job_messages import publish_job
        node, deployment = await self.node_fixture()
        self.settings.DEFAULT_LANGUAGE = 'en'
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=501), SimpleNamespace(message_id=502)]
        async with self.factory() as session:
            job = await self.service.enqueue(session, node, deployment, 'restart', 111)
            await session.commit()
        await publish_job(self.service, job.id)
        await publish_job(self.service, job.id)
        self.assertEqual(self.bot.send_message.await_count, 1)
        async with self.factory() as session:
            row = await session.get(Job, job.id)
            self.assertEqual(row.progress_message_id, 501)
            row.status, row.started_at = 'running', datetime.now(timezone.utc)
            await session.commit()
        restarted = InfrastructureService(self.settings, self.service.panel, self.bot, self.factory)
        await publish_job(restarted, job.id)
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs['message_id'], 501)
        async with self.factory() as session:
            row = await session.get(Job, job.id)
            row.status, row.finished_at = 'done', datetime.now(timezone.utc)
            await session.commit()
        await publish_job(restarted, job.id, complete=True)
        await publish_job(restarted, job.id, complete=True)
        self.assertEqual(self.bot.send_message.await_count, 2)
        text = self.bot.send_message.await_args.args[1]
        self.assertIn(node.name, text)
        self.assertIn('Restart', text)
        self.assertNotIn('#', text)
        self.assertNotIn('queued', text)
        async with self.factory() as session:
            self.assertEqual((await session.get(Job, job.id)).completion_message_id, 502)

    async def test_queue_handler_excludes_finished_jobs_and_numbers_waiting_jobs(self):
        from bot.handlers.admin.infrastructure import infrastructure_callback
        from bot.middlewares.i18n import JsonI18n
        node, deployment = await self.node_fixture()
        async with self.factory() as session:
            session.add_all([Job(node_id=node.id, deployment_id=deployment.id, requested_by=111,
                kind='restart', status=status) for status in ('done', 'running', 'pending', 'pending')])
            await session.commit()
        self.settings.INFRA_ENABLED = True
        message = SimpleNamespace(chat=SimpleNamespace(type='private'), edit_text=AsyncMock(), answer=AsyncMock())
        callback = SimpleNamespace(data='infra:jobs', from_user=SimpleNamespace(id=111), message=message, answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock(), clear=AsyncMock())
        async with self.factory() as session:
            await infrastructure_callback(callback, state, session, self.service, self.settings,
                {'current_language': 'en', 'i18n_instance': JsonI18n('locales', default='en')})
        rows = message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
        labels = [row[0].text for row in rows if row[0].callback_data.startswith('infra:job:')]
        self.assertEqual(len(labels), 3)
        self.assertTrue(labels[0].startswith('▶️'))
        self.assertTrue(labels[1].startswith('1.'))
        self.assertTrue(labels[2].startswith('2.'))

    async def test_prepare_uses_selected_install_profile_without_mutating_defaults(self):
        from unittest.mock import patch
        profile, inbound, provider = str(uuid4()), str(uuid4()), str(uuid4())
        defaults = {'profile_uuid': str(uuid4()), 'inbound_uuid': str(uuid4()), 'panel_ip': '203.0.113.10',
                    'sni_ru': [], 'sni_other': [], 'node_version_3': '3.4.2'}
        self.service.api.providers = AsyncMock(return_value=[{'uuid': provider}])
        self.service.api.nodes = AsyncMock(return_value=[])
        self.service.panel.api_major = AsyncMock(return_value=3)
        self.service.panel.get_inbounds_by_profile_uuid = AsyncMock(return_value=[{'uuid': inbound, 'port': 443, 'type': 'vless'}])
        values = {'address': '203.0.113.77', 'country': 'DE', 'provider_uuid': provider, 'period_days': 15,
                  'password': 'secret', 'host_public_key': 'test key', 'profile_uuid': profile, 'inbound_uuid': inbound}
        with patch('bot.services.infrastructure.service.read_config', new=AsyncMock(return_value=defaults)):
            async with self.factory() as session:
                node, job = await self.service.prepare(session, values, 111)
                await session.commit()
        async with self.factory() as session:
            saved = await session.get(Deployment, job.deployment_id)
            self.assertEqual(saved.profile_uuid, profile)
            self.assertEqual(saved.inbound_uuid, inbound)
        self.assertNotEqual(defaults['profile_uuid'], profile)
        self.service.panel.get_inbounds_by_profile_uuid.assert_awaited_with(profile, refresh=True)

    async def test_empty_and_obsolete_snapshots_use_each_admin_language(self):
        from db.dal import app_settings_dal
        english, fallback = self.user_id, self.user_id - 1
        self.settings.ADMIN_IDS = [english, fallback]
        self.settings.DEFAULT_LANGUAGE = 'ru'
        async with self.factory() as session:
            (await session.get(User, english)).language_code = 'en'
            for admin in self.settings.ADMIN_IDS:
                await app_settings_dal.upsert_setting(session, f'infra.snapshot.{admin}.{self.identity.scope}', '[11, 12]')
            await session.commit()
        await self.service.publish_snapshots()
        messages = {}
        for call in self.bot.edit_message_text.await_args_list:
            messages.setdefault(call.kwargs['chat_id'], []).append(call.args[0])
        self.assertEqual(messages[english], ['No saved SSH access.', 'The access list was updated in the preceding message.'])
        self.assertEqual(messages[fallback], ['Нет сохранённых доступов.', 'Список доступов обновлён в предыдущем сообщении.'])
        self.bot.send_message.assert_not_awaited()

    async def test_billing_reminder_and_button_use_recipient_language_and_escape_panel_text(self):
        from unittest.mock import patch
        from db.dal import app_settings_dal
        node, deployment = await self.node_fixture()
        node.panel_uuid = str(uuid4())
        english, fallback = self.user_id, self.user_id - 1
        self.settings.ADMIN_IDS = [english, fallback]
        self.settings.DEFAULT_LANGUAGE = 'ru'
        async with self.factory() as session:
            (await session.get(User, english)).language_code = 'en'
            stored = await session.get(Node, node.id)
            stored.name, stored.panel_uuid = 'node100 <RU>', node.panel_uuid
            await session.commit()
        self.service.api.providers = AsyncMock(return_value=[{'uuid': node.provider_uuid, 'name': 'Provider <test>'}])
        for overdue in (False, True):
            deadline = datetime.now(timezone.utc) + timedelta(hours=-1 if overdue else 2)
            self.service.api.billing = AsyncMock(return_value={'billingNodes': [{'uuid': str(uuid4()),
                'nodeUuid': node.panel_uuid, 'node': {'uuid': node.panel_uuid}, 'providerUuid': node.provider_uuid,
                'nextBillingAt': deadline.isoformat()}]})
            self.service._last_billing_check = 0
            self.bot.send_message.reset_mock()
            with patch('bot.services.infrastructure.service.read_config', new=AsyncMock(return_value={
                'warning_hours': [24], 'expire_exclude_tg': False})):
                await self.service.check_billing()
            messages = {call.args[0]: call for call in self.bot.send_message.await_args_list}
            self.assertEqual(set(messages), {english, fallback})
            self.assertIn('payment date has passed' if overdue else 'payment due in', messages[english].args[1])
            self.assertIn('дата оплаты прошла' if overdue else 'до оплаты', messages[fallback].args[1])
            for admin in (english, fallback):
                self.assertIn('node100 &lt;RU&gt;', messages[admin].args[1])
                self.assertIn('Provider &lt;test&gt;', messages[admin].args[1])
                self.assertEqual(messages[admin].kwargs['parse_mode'], 'HTML')
            self.assertEqual(messages[english].kwargs['reply_markup'].inline_keyboard[0][0].text, '🖥 Node')
            self.assertEqual(messages[fallback].kwargs['reply_markup'].inline_keyboard[0][0].text, '🖥 Нода')
        async with self.factory() as session:
            from db.models import AppSetting
            await session.execute(delete(AppSetting).where(AppSetting.key.like(f'infra.warn.{node.id}.%')))
            await session.commit()
