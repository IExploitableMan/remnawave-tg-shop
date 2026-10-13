import unittest
from types import SimpleNamespace
from bot.middlewares.i18n import JsonI18n
from bot.services.infrastructure.job_messages import operation_text, operation_buttons
from bot.services.infrastructure.versions import version_tuple


class OperationDisplayTests(unittest.TestCase):
    def test_running_test_progress_and_completed_actions_are_readable_in_both_languages(self):
        catalog = JsonI18n('locales', default='en')
        job = SimpleNamespace(id=123, node_id=1, kind='large', status='running',
            stage='test:7:30:2:3:udp', result=None, started_at=None, finished_at=None)
        node = SimpleNamespace(name='node25 <GE>')
        for language in ('ru', 'en'):
            translate = lambda key, **values: catalog.gettext(language, key, **values)
            text = operation_text(job, node, translate)
            self.assertIn('7', text)
            self.assertIn('30', text)
            self.assertIn('UDP', text)
            self.assertIn('node25 &lt;GE&gt;', text)
            self.assertNotIn('test:', text)
            self.assertNotIn('infra_', text)
        job.status, job.stage = 'done', 'queued'
        text = operation_text(job, node, translate)
        self.assertNotIn('queued', text)
        callbacks = [row[0].callback_data for row in operation_buttons(job, translate, 42).inline_keyboard]
        self.assertIn('infra:retry:123', callbacks)
        self.assertIn('infra:report:42', callbacks)

    def test_versions_are_compared_numerically_and_reject_nonrelease_tags(self):
        self.assertGreater(version_tuple('3.4.10'), version_tuple('v3.4.2'))
        self.assertIsNone(version_tuple('latest'))
        self.assertIsNone(version_tuple('3.4.3-beta'))

    def test_legacy_extra_checkpoint_does_not_inflate_progress(self):
        from bot.services.infrastructure.diagnostics import completed_checks, diagnostic_plan
        plan = diagnostic_plan('large', 2)
        results = [{'id': test[0], 'round': iteration} for iteration, test in plan]
        results += [{'id': 'ipquality', 'round': 1}, {'id': 'ipregion', 'round': 2}]
        self.assertEqual(completed_checks(results, plan), len(plan))
