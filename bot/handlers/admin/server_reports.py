import math
from datetime import datetime
from typing import Optional

from aiogram import F, Router, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.handlers.admin.user_management import (
    _send_with_profile_link_fallback,
    format_user_card,
    get_user_card_keyboard,
)
from bot.middlewares.i18n import JsonI18n
from bot.services.referral_service import ReferralService
from bot.services.server_report_service import get_issue_text
from bot.services.subscription_service import SubscriptionService
from config.settings import Settings
from db.dal import server_report_dal, user_dal

router = Router(name="admin_server_reports_router")

REPORTS_PAGE_SIZE = 8


def _format_dt(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "N/A"


def _user_label(user, user_id: int) -> str:
    if user and user.username:
        return f"@{user.username}"
    if user and user.first_name:
        return user.first_name
    return f"ID {user_id}"


def _host_summary(report) -> str:
    names = [host.host_name for host in report.hosts[:3]]
    if len(report.hosts) > 3:
        names.append(f"+{len(report.hosts) - 3}")
    return ", ".join(names) if names else "-"


async def show_server_reports_handler(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
    page: int = 0,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error displaying reports.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    total = await server_report_dal.count_reports(session)
    page = max(0, page)
    reports = await server_report_dal.get_recent_reports(
        session,
        limit=REPORTS_PAGE_SIZE,
        offset=page * REPORTS_PAGE_SIZE,
    )
    summary = await server_report_dal.get_report_summary(session)
    reports_enabled = await server_report_dal.get_admin_reports_enabled(session, callback.from_user.id)

    by_issue = summary.get("by_issue") or {}
    by_issue_text = ", ".join(
        f"{get_issue_text(i18n, current_lang, issue)}: {count}"
        for issue, count in by_issue.items()
    ) or "-"
    top_hosts = summary.get("top_hosts") or []
    top_hosts_text = ", ".join(f"{host}: {count}" for host, count in top_hosts) or "-"

    text = _(
        "admin_server_reports_text",
        total=summary.get("total", 0),
        last_24h=summary.get("last_24h", 0),
        by_issue=by_issue_text,
        top_hosts=top_hosts_text,
        notifications=_("admin_server_reports_notifications_on" if reports_enabled else "admin_server_reports_notifications_off"),
    )

    builder = InlineKeyboardBuilder()
    for report in reports:
        label = _(
            "admin_server_report_list_button",
            report_id=report.report_id,
            time=_format_dt(report.created_at),
            user=_user_label(report.user, report.user_id),
            issue=get_issue_text(i18n, current_lang, report.issue_type),
        )
        builder.button(text=label[:64], callback_data=f"admin_report:view:{report.report_id}:{page}")

    total_pages = max(1, math.ceil(total / REPORTS_PAGE_SIZE))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text=_("prev_page_button"), callback_data=f"admin_reports:page:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(types.InlineKeyboardButton(text=_("next_page_button"), callback_data=f"admin_reports:page:{page + 1}"))
    if nav:
        builder.row(*nav)

    toggle_key = "admin_server_reports_disable_notifications" if reports_enabled else "admin_server_reports_enable_notifications"
    builder.button(text=_(toggle_key), callback_data=f"admin_reports:toggle_notify:{page}")
    builder.button(text=_("back_to_stats_monitoring_button"), callback_data="admin_section:stats_monitoring")
    builder.adjust(1)

    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_reports:page:"))
async def reports_page_callback(callback: types.CallbackQuery, i18n_data: dict, settings: Settings, session: AsyncSession):
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    await show_server_reports_handler(callback, i18n_data, settings, session, page=page)


@router.callback_query(F.data.startswith("admin_reports:toggle_notify:"))
async def toggle_notifications_callback(callback: types.CallbackQuery, i18n_data: dict, settings: Settings, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await callback.answer("Error", show_alert=True)
        return
    try:
        page = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        page = 0
    current = await server_report_dal.get_admin_reports_enabled(session, callback.from_user.id)
    await server_report_dal.set_admin_reports_enabled(session, callback.from_user.id, not current)
    await session.commit()
    await callback.answer(
        i18n.gettext(
            current_lang,
            "admin_server_reports_notifications_enabled_alert" if not current else "admin_server_reports_notifications_disabled_alert",
        )
    )
    await show_server_reports_handler(callback, i18n_data, settings, session, page=page)


@router.callback_query(F.data.startswith("admin_report:view:"))
async def report_detail_callback(callback: types.CallbackQuery, i18n_data: dict, settings: Settings, session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    try:
        _, _, report_id_raw, page_raw = callback.data.split(":")
        report_id = int(report_id_raw)
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    report = await server_report_dal.get_report_by_id(session, report_id)
    if not report:
        await callback.answer(_("admin_server_report_not_found"), show_alert=True)
        return
    await server_report_dal.mark_report_seen(session, report_id)
    await session.commit()

    hosts_text = "\n".join(
        _(
            "admin_server_report_host_line",
            host=host.host_name,
            address=host.host_address or "-",
            node=host.node_name or "-",
            profile=host.profile_kind or "-",
        )
        for host in report.hosts
    ) or "-"
    text = _(
        "admin_server_report_detail_text",
        report_id=report.report_id,
        time=_format_dt(report.created_at),
        user=_user_label(report.user, report.user_id),
        user_id=report.user_id,
        issue=get_issue_text(i18n, current_lang, report.issue_type),
        hosts=hosts_text,
    )

    builder = InlineKeyboardBuilder()
    builder.button(text=_("user_card_open_profile_button"), callback_data=f"admin_report_user:{report.user_id}:{page}")
    builder.button(text=_("back_to_server_reports_button"), callback_data=f"admin_reports:page:{page}")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_report_user:"))
async def report_user_card_callback(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    bot,
    subscription_service: SubscriptionService,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    try:
        _, user_id_raw, page_raw = callback.data.split(":")
        user_id = int(user_id_raw)
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    user = await user_dal.get_user_by_id(session, user_id)
    if not user:
        await callback.answer(_("admin_user_not_found_alert"), show_alert=True)
        return

    referral_service = ReferralService(settings, subscription_service, bot, i18n)
    user_card_text = await format_user_card(user, session, subscription_service, i18n, current_lang, referral_service)
    keyboard = get_user_card_keyboard(user_id, i18n, current_lang, user.referred_by_id)
    keyboard.button(text=_("back_to_server_reports_button"), callback_data=f"admin_reports:page:{page}")
    keyboard.adjust(2, 2, 2, 2, 1, 1, 2, 1)

    await _send_with_profile_link_fallback(
        callback.message.edit_text,
        text=user_card_text,
        markup=keyboard.as_markup(),
        user_id=user_id,
        parse_mode="HTML",
    )
    await callback.answer()

