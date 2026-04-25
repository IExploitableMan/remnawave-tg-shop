import unittest
from pathlib import Path


class AdminServerReportsSourceTests(unittest.TestCase):
    def test_handlers_do_not_shadow_translation_lambda_with_split_placeholders(self):
        source = Path("bot/handlers/admin/server_reports.py").read_text(encoding="utf-8")

        self.assertNotIn("_, _, report_id_raw, page_raw = callback.data.split", source)
        self.assertNotIn("_, user_id_raw, page_raw = callback.data.split", source)
