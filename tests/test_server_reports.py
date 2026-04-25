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
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "11111111-1111-4111-8111-111111111111",
                },
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
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "11111111-1111-4111-8111-111111111111",
                },
            },
        ]
        accessible_nodes = [
            {
                "uuid": "node-1",
                "nodeName": "Node DE",
                "configProfileUuid": "profile-1",
                "activeSquads": [
                    {
                        "activeInbounds": ["11111111-1111-4111-8111-111111111111"],
                    }
                ],
            }
        ]

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

    def test_filter_hosts_for_accessible_nodes_respects_inbound_and_excluded_squads(self):
        hosts = [
            {
                "uuid": "host-1",
                "remark": "Allowed",
                "address": "allowed.example.com",
                "nodes": ["node-1"],
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "11111111-1111-4111-8111-111111111111",
                },
                "excludedInternalSquads": [],
            },
            {
                "uuid": "host-2",
                "remark": "Wrong inbound",
                "address": "wrong-inbound.example.com",
                "nodes": ["node-1"],
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "22222222-2222-4222-8222-222222222222",
                },
                "excludedInternalSquads": [],
            },
            {
                "uuid": "host-3",
                "remark": "Excluded squad",
                "address": "excluded.example.com",
                "nodes": ["node-1"],
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "11111111-1111-4111-8111-111111111111",
                },
                "excludedInternalSquads": ["squad-1"],
            },
        ]
        accessible_nodes = [
            {
                "uuid": "node-1",
                "nodeName": "Node DE",
                "configProfileUuid": "profile-1",
                "activeSquads": [
                    {
                        "activeInbounds": ["11111111-1111-4111-8111-111111111111"],
                    }
                ],
            }
        ]

        result = ServerReportService._filter_hosts_for_nodes(
            hosts,
            accessible_nodes,
            active_internal_squad_uuids={"squad-1"},
            profile_kind="base",
        )

        self.assertEqual([item["host_uuid"] for item in result], ["host-1"])

    def test_filter_hosts_for_accessible_nodes_fails_closed_when_inbounds_are_unresolved_tags(self):
        hosts = [
            {
                "uuid": "host-1",
                "remark": "Allowed by profile",
                "address": "allowed.example.com",
                "nodes": ["node-1"],
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "11111111-1111-4111-8111-111111111111",
                },
                "excludedInternalSquads": [],
            }
        ]
        accessible_nodes = [
            {
                "uuid": "node-1",
                "nodeName": "Node DE",
                "configProfileUuid": "profile-1",
                "activeSquads": [
                    {
                        "activeInbounds": ["vless-reality-main"],
                    }
                ],
            }
        ]

        result = ServerReportService._filter_hosts_for_nodes(
            hosts,
            accessible_nodes,
            active_internal_squad_uuids=set(),
            profile_kind="base",
        )

        self.assertEqual(result, [])

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


class ServerReportServiceNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_squad_scoped_nodes_are_wrapped_for_normalization(self):
        panel_service = SimpleNamespace(
            get_internal_squad_accessible_nodes=self._fake_get_internal_squad_accessible_nodes,
            get_user_accessible_nodes=self._unexpected_get_user_accessible_nodes,
        )
        service = ServerReportService(
            settings=SimpleNamespace(ADMIN_IDS=[]),
            panel_service=panel_service,
            bot=None,
            i18n=None,
        )

        nodes = await service._get_accessible_nodes_for_panel_user(
            "panel-user-1",
            {"squad-basic"},
        )

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["activeSquads"][0]["squadUuid"], "squad-basic")
        self.assertEqual(nodes[0]["activeSquads"][0]["activeInbounds"], ["vless-basic"])

    async def test_normalize_accessible_nodes_resolves_inbound_tags_to_uuids(self):
        panel_service = SimpleNamespace(
            get_inbounds_by_profile_uuid=self._fake_get_inbounds_by_profile_uuid,
        )
        service = ServerReportService(
            settings=SimpleNamespace(ADMIN_IDS=[]),
            panel_service=panel_service,
            bot=None,
            i18n=None,
        )

        nodes = [
            {
                "uuid": "node-1",
                "nodeName": "Node DE",
                "configProfileUuid": "profile-1",
                "activeSquads": [
                    {
                        "activeInbounds": ["vless-basic", "trojan-basic"],
                    }
                ],
            }
        ]

        normalized = await service._normalize_accessible_nodes(nodes)

        self.assertEqual(
            normalized[0]["activeSquads"][0]["activeInbounds"],
            [
                "11111111-1111-4111-8111-111111111111",
                "33333333-3333-4333-8333-333333333333",
            ],
        )

    async def test_normalize_accessible_nodes_drops_unresolved_tag_squads(self):
        panel_service = SimpleNamespace(
            get_inbounds_by_profile_uuid=self._fake_get_inbounds_by_profile_uuid,
        )
        service = ServerReportService(
            settings=SimpleNamespace(ADMIN_IDS=[]),
            panel_service=panel_service,
            bot=None,
            i18n=None,
        )

        nodes = [
            {
                "uuid": "node-1",
                "nodeName": "Node DE",
                "configProfileUuid": "profile-1",
                "activeSquads": [
                    {
                        "activeInbounds": ["unknown-tag"],
                    }
                ],
            }
        ]

        normalized = await service._normalize_accessible_nodes(nodes)

        self.assertEqual(normalized[0]["activeSquads"], [])

    async def test_normalized_tag_inbounds_filter_out_other_squad_hosts(self):
        panel_service = SimpleNamespace(
            get_inbounds_by_profile_uuid=self._fake_get_inbounds_by_profile_uuid,
        )
        service = ServerReportService(
            settings=SimpleNamespace(ADMIN_IDS=[]),
            panel_service=panel_service,
            bot=None,
            i18n=None,
        )
        nodes = [
            {
                "uuid": "node-1",
                "nodeName": "Node DE",
                "configProfileUuid": "profile-1",
                "activeSquads": [
                    {
                        "activeInbounds": ["vless-basic"],
                    }
                ],
            }
        ]
        hosts = [
            {
                "uuid": "base-host",
                "remark": "Base",
                "address": "base.example.com",
                "nodes": ["node-1"],
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "11111111-1111-4111-8111-111111111111",
                },
            },
            {
                "uuid": "whitelist-host",
                "remark": "Whitelist",
                "address": "wl.example.com",
                "nodes": ["node-1"],
                "inbound": {
                    "configProfileUuid": "profile-1",
                    "configProfileInboundUuid": "22222222-2222-4222-8222-222222222222",
                },
            },
        ]

        normalized = await service._normalize_accessible_nodes(nodes)
        result = ServerReportService._filter_hosts_for_nodes(
            hosts,
            normalized,
            active_internal_squad_uuids={"squad-basic"},
            profile_kind="base",
        )

        self.assertEqual([item["host_uuid"] for item in result], ["base-host"])

    @staticmethod
    async def _fake_get_inbounds_by_profile_uuid(profile_uuid):
        if profile_uuid != "profile-1":
            return []
        return [
            {
                "uuid": "11111111-1111-4111-8111-111111111111",
                "tag": "vless-basic",
            },
            {
                "uuid": "22222222-2222-4222-8222-222222222222",
                "tag": "vless-whitelist",
            },
            {
                "uuid": "33333333-3333-4333-8333-333333333333",
                "tag": "trojan-basic",
            },
        ]

    @staticmethod
    async def _fake_get_internal_squad_accessible_nodes(squad_uuid):
        if squad_uuid != "squad-basic":
            return []
        return [
            {
                "uuid": "node-1",
                "nodeName": "Node DE",
                "configProfileUuid": "profile-1",
                "activeInbounds": ["vless-basic"],
            }
        ]

    @staticmethod
    async def _unexpected_get_user_accessible_nodes(_panel_uuid):
        raise AssertionError("user-wide accessible nodes must not be used when panel user has squads")
