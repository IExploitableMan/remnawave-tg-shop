import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.panel_api_service import PanelApiService
from db.dal import server_report_dal, subscription_dal, user_dal
from bot.utils.product_kinds import SUBSCRIPTION_KIND_ADDON, SUBSCRIPTION_KIND_BASE


ISSUE_TYPE_KEYS = {
    "slow": "server_report_issue_slow",
    "connect": "server_report_issue_connect",
    "sites": "server_report_issue_sites",
    "unstable": "server_report_issue_unstable",
    "other": "server_report_issue_other",
}

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def get_issue_text(i18n: JsonI18n, lang: str, issue_type: str) -> str:
    key = ISSUE_TYPE_KEYS.get(issue_type, "server_report_issue_other")
    return i18n.gettext(lang, key)


def report_cooldown_until(last_report, cooldown_hours: int) -> Optional[datetime]:
    if not last_report or not last_report.created_at or cooldown_hours <= 0:
        return None
    until = last_report.created_at + timedelta(hours=cooldown_hours)
    return until if until > datetime.now(timezone.utc) else None


class ServerReportService:
    def __init__(
        self,
        *,
        settings: Settings,
        panel_service: PanelApiService,
        bot: Bot,
        i18n: JsonI18n,
    ) -> None:
        self.settings = settings
        self.panel_service = panel_service
        self.bot = bot
        self.i18n = i18n

    async def get_available_hosts_for_user(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> list[dict[str, Any]]:
        profile_uuids = await self._get_active_profile_uuids(session, user_id)
        if not profile_uuids:
            return []

        panel_hosts = await self.panel_service.get_all_hosts()
        if not panel_hosts:
            return []

        all_hosts_by_uuid: dict[str, dict[str, Any]] = {}
        for profile_kind, panel_uuid in profile_uuids:
            panel_user = await self.panel_service.get_user_by_uuid(panel_uuid, log_response=False)
            active_internal_squad_uuids = self._extract_active_internal_squad_uuids(panel_user)
            nodes = await self._get_accessible_nodes_for_panel_user(
                panel_uuid,
                active_internal_squad_uuids,
            )
            if not nodes:
                continue
            normalized_nodes = await self._normalize_accessible_nodes(nodes)
            visible_for_profile = self._filter_hosts_for_nodes(
                panel_hosts,
                normalized_nodes,
                active_internal_squad_uuids=active_internal_squad_uuids,
                profile_kind=profile_kind,
            )
            for host in visible_for_profile:
                existing = all_hosts_by_uuid.get(host["host_uuid"])
                if existing:
                    kinds = set((existing.get("profile_kind") or "").split("+"))
                    kinds.add(profile_kind)
                    existing["profile_kind"] = "+".join(sorted(k for k in kinds if k))
                    continue
                all_hosts_by_uuid[host["host_uuid"]] = host

        return sorted(
            all_hosts_by_uuid.values(),
            key=lambda item: (item.get("host_name") or "", item.get("host_uuid") or ""),
        )

    async def _get_accessible_nodes_for_panel_user(
        self,
        panel_uuid: str,
        active_internal_squad_uuids: set[str],
    ) -> list[dict[str, Any]]:
        if active_internal_squad_uuids:
            nodes_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for squad_uuid in sorted(active_internal_squad_uuids):
                squad_nodes = await self.panel_service.get_internal_squad_accessible_nodes(squad_uuid)
                for node in squad_nodes or []:
                    normalized_node = self._wrap_internal_squad_node(node, squad_uuid)
                    key = (
                        str(normalized_node.get("uuid") or ""),
                        str(normalized_node.get("configProfileUuid") or ""),
                    )
                    existing = nodes_by_key.get(key)
                    if not existing:
                        nodes_by_key[key] = normalized_node
                        continue
                    existing.setdefault("activeSquads", []).extend(
                        normalized_node.get("activeSquads") or []
                    )
            return list(nodes_by_key.values())

        logging.warning(
            "Panel user %s has no activeInternalSquads; falling back to user accessible nodes.",
            panel_uuid,
        )
        nodes = await self.panel_service.get_user_accessible_nodes(panel_uuid)
        return nodes or []

    @staticmethod
    def _wrap_internal_squad_node(node: dict[str, Any], squad_uuid: str) -> dict[str, Any]:
        return {
            **node,
            "activeSquads": [
                {
                    "squadUuid": squad_uuid,
                    "activeInbounds": list(node.get("activeInbounds") or []),
                }
            ],
        }

    async def _normalize_accessible_nodes(
        self,
        accessible_nodes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_nodes: list[dict[str, Any]] = []
        for node in accessible_nodes:
            config_profile_uuid = str(node.get("configProfileUuid") or "")
            normalized_squads: list[dict[str, Any]] = []
            for squad in node.get("activeSquads") or []:
                raw_inbounds = [str(value) for value in (squad.get("activeInbounds") or []) if value]
                if raw_inbounds and all(UUID_RE.match(value) for value in raw_inbounds):
                    normalized_squads.append(
                        {
                            **squad,
                            "activeInbounds": raw_inbounds,
                        }
                    )
                    continue

                resolved_inbound_uuids = await self._resolve_inbound_values_to_uuids(
                    config_profile_uuid,
                    raw_inbounds,
                )
                if raw_inbounds and not resolved_inbound_uuids:
                    logging.warning(
                        "Skipping unresolved accessible node squad for profile %s. Raw inbounds: %s",
                        config_profile_uuid or "N/A",
                        raw_inbounds,
                    )
                    continue
                normalized_squads.append(
                    {
                        **squad,
                        "activeInbounds": resolved_inbound_uuids,
                    }
                )
            normalized_nodes.append(
                {
                    **node,
                    "activeSquads": normalized_squads,
                }
            )
        return normalized_nodes

    async def _resolve_inbound_values_to_uuids(
        self,
        config_profile_uuid: str,
        raw_inbounds: list[str],
    ) -> list[str]:
        if not raw_inbounds:
            return []
        if all(UUID_RE.match(value) for value in raw_inbounds):
            return raw_inbounds
        if not config_profile_uuid:
            return []

        profile_inbounds = await self.panel_service.get_inbounds_by_profile_uuid(config_profile_uuid)
        if not profile_inbounds:
            return []

        inbound_uuid_by_tag = {
            str(inbound.get("tag")): str(inbound.get("uuid"))
            for inbound in profile_inbounds
            if inbound.get("tag") and inbound.get("uuid")
        }
        inbound_uuid_set = {
            str(inbound.get("uuid"))
            for inbound in profile_inbounds
            if inbound.get("uuid")
        }

        resolved: list[str] = []
        for value in raw_inbounds:
            if value in inbound_uuid_set:
                resolved.append(value)
                continue
            mapped_uuid = inbound_uuid_by_tag.get(value)
            if mapped_uuid:
                resolved.append(mapped_uuid)
        return resolved

    async def create_report(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        issue_type: str,
        selected_hosts: list[dict[str, Any]],
        details: Optional[str] = None,
    ):
        report = await server_report_dal.create_server_report(
            session,
            user_id=user_id,
            issue_type=issue_type,
            details=details,
            hosts=selected_hosts,
        )
        await session.commit()
        report = await server_report_dal.get_report_by_id(session, report.report_id)
        if report:
            await self.notify_admins(session, report)
        return report

    async def notify_admins(self, session: AsyncSession, report) -> None:
        admin_ids = await server_report_dal.get_enabled_admin_ids(session, self.settings.ADMIN_IDS)
        if not admin_ids:
            return

        db_user = report.user or await user_dal.get_user_by_id(session, report.user_id)
        user_label = self._format_user_label(db_user, report.user_id)
        host_lines = "\n".join(f"- {host.host_name}" for host in report.hosts) or "-"
        for admin_id in admin_ids:
            lang = self.settings.DEFAULT_LANGUAGE
            text = self.i18n.gettext(
                lang,
                "admin_server_report_notification",
                report_id=report.report_id,
                user=user_label,
                user_id=report.user_id,
                issue=get_issue_text(self.i18n, lang, report.issue_type),
                details=escape(report.details or "-"),
                hosts=host_lines,
            )
            builder = InlineKeyboardBuilder()
            builder.button(
                text=self.i18n.gettext(lang, "admin_server_reports_open_button"),
                callback_data="admin_action:server_reports",
            )
            builder.button(
                text=self.i18n.gettext(lang, "user_card_open_profile_button"),
                callback_data=f"admin_report_user:{report.user_id}:0",
            )
            builder.adjust(1)
            try:
                await self.bot.send_message(admin_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")
            except Exception:
                logging.exception("Failed to send server report notification to admin %s", admin_id)

    async def _get_active_profile_uuids(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> list[tuple[str, str]]:
        profiles: list[tuple[str, str]] = []
        base_sub = await subscription_dal.get_active_subscription_by_user_id(
            session,
            user_id,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        addon_sub = await subscription_dal.get_active_subscription_by_user_id(
            session,
            user_id,
            kind=SUBSCRIPTION_KIND_ADDON,
        )
        if base_sub and base_sub.panel_user_uuid:
            profiles.append((SUBSCRIPTION_KIND_BASE, base_sub.panel_user_uuid))
        if addon_sub and addon_sub.panel_user_uuid:
            profiles.append((SUBSCRIPTION_KIND_ADDON, addon_sub.panel_user_uuid))
        return profiles

    @staticmethod
    def _filter_hosts_for_nodes(
        panel_hosts: list[dict[str, Any]],
        accessible_nodes: list[dict[str, Any]],
        *,
        active_internal_squad_uuids: Optional[set[str]] = None,
        profile_kind: str,
    ) -> list[dict[str, Any]]:
        node_meta: dict[str, dict[str, Any]] = {}
        for node in accessible_nodes:
            node_uuid = str(node.get("uuid") or "")
            if not node_uuid:
                continue
            allowed_inbounds: set[str] = set()
            for squad in node.get("activeSquads") or []:
                for inbound_uuid in squad.get("activeInbounds") or []:
                    if inbound_uuid:
                        allowed_inbounds.add(str(inbound_uuid))
            if not allowed_inbounds:
                continue
            node_meta[node_uuid] = {
                "node_name": node.get("nodeName"),
                "config_profile_uuid": str(node.get("configProfileUuid") or ""),
                "allowed_inbounds": allowed_inbounds,
            }
        allowed_node_uuids = set(node_meta.keys())
        result: list[dict[str, Any]] = []

        for host in panel_hosts:
            if host.get("isDisabled") or host.get("isHidden"):
                continue
            excluded_internal_squads = {
                str(squad_uuid)
                for squad_uuid in (host.get("excludedInternalSquads") or [])
                if squad_uuid
            }
            if (
                active_internal_squad_uuids
                and excluded_internal_squads
                and active_internal_squad_uuids.intersection(excluded_internal_squads)
            ):
                continue
            host_nodes = [str(node_uuid) for node_uuid in (host.get("nodes") or []) if node_uuid]
            inbound = host.get("inbound") or {}
            host_profile_uuid = str(inbound.get("configProfileUuid") or "")
            host_inbound_uuid = str(inbound.get("configProfileInboundUuid") or "")
            matched_node_uuid = None
            for node_uuid in host_nodes:
                if node_uuid not in allowed_node_uuids:
                    continue
                meta = node_meta.get(node_uuid) or {}
                node_profile_uuid = meta.get("config_profile_uuid") or ""
                if host_profile_uuid and node_profile_uuid and host_profile_uuid != node_profile_uuid:
                    continue
                allowed_inbounds = meta.get("allowed_inbounds") or set()
                if (
                    allowed_inbounds
                    and (not host_inbound_uuid or host_inbound_uuid not in allowed_inbounds)
                ):
                    continue
                matched_node_uuid = node_uuid
                break
            if not matched_node_uuid:
                continue
            host_name = (
                host.get("serverDescription")
                or host.get("remark")
                or host.get("address")
                or host.get("uuid")
                or "Unknown"
            )
            result.append(
                {
                    "host_uuid": str(host.get("uuid") or ""),
                    "host_name": str(host_name),
                    "host_address": host.get("address"),
                    "node_uuid": matched_node_uuid,
                    "node_name": (node_meta.get(matched_node_uuid) or {}).get("node_name"),
                    "profile_kind": profile_kind,
                }
            )
        return result

    @staticmethod
    def _extract_active_internal_squad_uuids(panel_user: Optional[dict[str, Any]]) -> set[str]:
        if not panel_user:
            return set()
        return {
            str(squad.get("uuid"))
            for squad in (panel_user.get("activeInternalSquads") or [])
            if squad and squad.get("uuid")
        }

    @staticmethod
    def _format_user_label(user, user_id: int) -> str:
        if not user:
            return f"ID {user_id}"
        if user.username:
            return f"@{user.username}"
        if user.first_name:
            return user.first_name
        return f"ID {user_id}"
