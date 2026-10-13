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

    async def test_database_prevents_parallel_active_jobs(self):
        node, deployment = await self.node_fixture()
        deployment.state = 'replaced'
        async with self.factory() as session:
            with self.assertRaises(ValueError):
                await self.service.enqueue(session, node, deployment, 'provision', 111)
        deployment.state = 'active'
        async with self.factory() as session:
            await self.service.enqueue(session, node, deployment, 'small', 111)
            await session.commit()
        async with self.factory() as session:
            with self.assertRaises(ValueError):
                await self.service.enqueue(session, node, deployment, 'large', 111)
        async with self.factory() as session:
            session.add(Job(node_id=node.id, deployment_id=deployment.id, kind='small', requested_by=222))
            with self.assertRaises(IntegrityError):
                await session.commit()
            await session.rollback()

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
        async with self.factory() as session:
            (await session.get(Deployment, deployment.id)).node_version = '2.8.0'
            job = await self.service.enqueue(session, node, deployment, 'upgrade', 111)
            await session.commit()
        with patch('bot.services.infrastructure.provisioning.ansible', new=AsyncMock(side_effect=RuntimeError('Installation failed'))):
            await run_job(self.service, job.id)
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
