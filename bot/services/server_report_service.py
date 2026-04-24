import logging
from datetime import datetime, timedelta, timezone
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
            nodes = await self.panel_service.get_user_accessible_nodes(panel_uuid)
            if not nodes:
                continue
            visible_for_profile = self._filter_hosts_for_nodes(
                panel_hosts,
                nodes,
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

    async def create_report(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        issue_type: str,
        selected_hosts: list[dict[str, Any]],
    ):
        report = await server_report_dal.create_server_report(
            session,
            user_id=user_id,
            issue_type=issue_type,
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
        profile_kind: str,
    ) -> list[dict[str, Any]]:
        node_names = {str(node.get("uuid")): node.get("nodeName") for node in accessible_nodes if node.get("uuid")}
        allowed_node_uuids = set(node_names.keys())
        result: list[dict[str, Any]] = []

        for host in panel_hosts:
            if host.get("isDisabled") or host.get("isHidden"):
                continue
            host_nodes = [str(node_uuid) for node_uuid in (host.get("nodes") or []) if node_uuid]
            matched_node_uuid = next((node_uuid for node_uuid in host_nodes if node_uuid in allowed_node_uuids), None)
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
                    "node_name": node_names.get(matched_node_uuid),
                    "profile_kind": profile_kind,
                }
            )
        return result

    @staticmethod
    def _format_user_label(user, user_id: int) -> str:
        if not user:
            return f"ID {user_id}"
        if user.username:
            return f"@{user.username}"
        if user.first_name:
            return user.first_name
        return f"ID {user_id}"

