import unittest
from types import SimpleNamespace

try:
    from bot.handlers.admin.logs_admin import _format_log_action, _format_log_entry_text
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        raise unittest.SkipTest(
            f"Project runtime dependency is not installed: {exc.name}"
        ) from exc
    raise


class FakeI18n:
    def gettext(self, _lang, key, **_kwargs):
        values = {
            "admin_log_action_label": "Action:",
            "admin_log_payload_label": "Payload:",
            "admin_log_details_label": "Details:",
            "admin_log_details_unavailable": "details unavailable",
            "admin_log_details_from_raw": "extracted from raw update",
            "system_or_unknown_user": "System",
        }
        return values.get(key, key)


class AdminLogsFormattingTests(unittest.TestCase):
    def test_format_log_action_uses_full_callback_payload(self):
        log = SimpleNamespace(
            event_type="callback:main_action:my_subscription",
            content=None,
            raw_update_preview=None,
        )

        action_label, payload_value, raw_tail = _format_log_action(log)

        self.assertEqual(action_label, "Callback: Main Action -> My Subscription")
        self.assertEqual(payload_value, "main_action:my_subscription")
        self.assertIsNone(raw_tail)

    def test_format_log_entry_text_falls_back_to_raw_update_payload(self):
        log = SimpleNamespace(
            telegram_first_name="Max",
            telegram_username="minenooz",
            user_id=123,
            timestamp=None,
            event_type="callback:main_action",
            content=None,
            raw_update_preview='{"callback_query":{"data":"main_action:server_report"}}',
        )

        text = _format_log_entry_text(log, FakeI18n(), "en")

        self.assertIn("Callback: Main Action -&gt; Server Report", text)
        self.assertIn("main_action:server_report", text)

    def test_format_log_entry_text_uses_event_payload_when_only_event_type_exists(self):
        log = SimpleNamespace(
            telegram_first_name="Max",
            telegram_username=None,
            user_id=123,
            timestamp=None,
            event_type="callback:main_action",
            content=None,
            raw_update_preview=None,
        )

        text = _format_log_entry_text(log, FakeI18n(), "en")

        self.assertIn("Callback: Main Action", text)
        self.assertIn("<code>main_action</code>", text)
