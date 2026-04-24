import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

try:
    from bot.services.server_report_service import ServerReportService, report_cooldown_until
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        raise unittest.SkipTest(
            f"Project runtime dependency is not installed: {exc.name}"
        ) from exc
    raise


class ServerReportServiceTests(unittest.TestCase):
    def test_filter_hosts_for_accessible_nodes_skips_hidden_disabled_and_unavailable(self):
        hosts = [
            {
                "uuid": "host-1",
                "remark": "Germany",
                "address": "de.example.com",
                "nodes": ["node-1"],
                "isDisabled": False,
                "isHidden": False,
            },
            {
                "uuid": "host-2",
                "remark": "Hidden",
                "address": "hidden.example.com",
                "nodes": ["node-1"],
                "isHidden": True,
            },
            {
                "uuid": "host-3",
                "remark": "Disabled",
                "address": "disabled.example.com",
                "nodes": ["node-1"],
                "isDisabled": True,
            },
            {
                "uuid": "host-4",
                "remark": "Other node",
                "address": "other.example.com",
                "nodes": ["node-2"],
            },
        ]
        accessible_nodes = [{"uuid": "node-1", "nodeName": "Node DE"}]

        result = ServerReportService._filter_hosts_for_nodes(
            hosts,
            accessible_nodes,
            profile_kind="base",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["host_uuid"], "host-1")
        self.assertEqual(result[0]["host_name"], "Germany")
        self.assertEqual(result[0]["node_name"], "Node DE")
        self.assertEqual(result[0]["profile_kind"], "base")

    def test_report_cooldown_until_returns_future_deadline(self):
        last_report = SimpleNamespace(
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )

        until = report_cooldown_until(last_report, 24)

        self.assertIsNotNone(until)
        self.assertGreater(until, datetime.now(timezone.utc))

    def test_report_cooldown_until_allows_after_deadline(self):
        last_report = SimpleNamespace(
            created_at=datetime.now(timezone.utc) - timedelta(hours=25),
        )

        self.assertIsNone(report_cooldown_until(last_report, 24))

