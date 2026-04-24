from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline.user_keyboards import get_back_to_main_menu_markup
from bot.middlewares.i18n import JsonI18n
from bot.services.panel_api_service import PanelApiService
from bot.services.server_report_service import (
    ISSUE_TYPE_KEYS,
    ServerReportService,
    get_issue_text,
    report_cooldown_until,
)
from config.settings import Settings
from db.dal import server_report_dal

router = Router(name="user_server_report_router")


def _format_cooldown(until: datetime) -> str:
    delta = until - datetime.now(timezone.utc)
    seconds = max(0, int(delta.total_seconds()))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_issue_keyboard(lang: str, i18n: JsonI18n):
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    for issue_code, key in ISSUE_TYPE_KEYS.items():
        builder.button(text=_(key), callback_data=f"server_report_issue:{issue_code}")
    builder.button(text=_("back_to_main_menu_button"), callback_data="main_action:back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def get_hosts_keyboard(lang: str, i18n: JsonI18n, hosts: list[dict], selected: set[int]):
    _ = lambda key, **kwargs: i18n.gettext(lang, key, **kwargs)
    builder = InlineKeyboardBuilder()
    for idx, host in enumerate(hosts):
        mark = "☑" if idx in selected else "☐"
        text = f"{mark} {host.get('host_name') or host.get('host_uuid')}"
        builder.button(text=text[:64], callback_data=f"server_report_host:{idx}")
    builder.button(text=_("server_report_submit_button"), callback_data="server_report_submit")
    builder.button(text=_("back_button"), callback_data="server_report_back_to_issue")
    builder.button(text=_("cancel_button"), callback_data="main_action:back_to_main")
    builder.adjust(1)
    return builder.as_markup()


async def start_server_report_flow(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Language error.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    last_report = await server_report_dal.get_last_report_by_user(session, callback.from_user.id)
    cooldown_until = report_cooldown_until(last_report, settings.SERVER_REPORT_COOLDOWN_HOURS)
    if cooldown_until:
        await callback.answer(
            _("server_report_cooldown_alert", time_left=_format_cooldown(cooldown_until)),
            show_alert=True,
        )
        return

    await state.update_data(server_report_issue=None, server_report_hosts=[], server_report_selected=[])
    await callback.message.edit_text(
        _("server_report_issue_prompt"),
        reply_markup=get_issue_keyboard(current_lang, i18n),
    )
    await callback.answer()


async def show_cooldown_alert(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n:
        await callback.answer("Rate limit active.", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    last_report = await server_report_dal.get_last_report_by_user(session, callback.from_user.id)
    cooldown_until = report_cooldown_until(last_report, settings.SERVER_REPORT_COOLDOWN_HOURS)
    time_left = _format_cooldown(cooldown_until) if cooldown_until else "0m"
    await callback.answer(_("server_report_cooldown_alert", time_left=time_left), show_alert=True)


@router.callback_query(F.data.startswith("server_report_issue:"))
async def select_issue_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    bot: Bot,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
    issue_code = callback.data.split(":", 1)[1]
    if issue_code not in ISSUE_TYPE_KEYS:
        await callback.answer(_("error_try_again"), show_alert=True)
        return

    service = ServerReportService(settings=settings, panel_service=panel_service, bot=bot, i18n=i18n)
    hosts = await service.get_available_hosts_for_user(session, callback.from_user.id)
    if not hosts:
        await callback.message.edit_text(
            _("server_report_no_hosts"),
            reply_markup=get_back_to_main_menu_markup(current_lang, i18n),
        )
        await callback.answer()
        return

    await state.update_data(
        server_report_issue=issue_code,
        server_report_hosts=hosts,
        server_report_selected=[],
    )
    await callback.message.edit_text(
        _("server_report_hosts_prompt", issue=get_issue_text(i18n, current_lang, issue_code)),
        reply_markup=get_hosts_keyboard(current_lang, i18n, hosts, set()),
    )
    await callback.answer()


@router.callback_query(F.data == "server_report_back_to_issue")
async def back_to_issue_callback(callback: types.CallbackQuery, i18n_data: dict):
    current_lang = i18n_data.get("current_language", "ru")
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error", show_alert=True)
        return
    await callback.message.edit_text(
        i18n.gettext(current_lang, "server_report_issue_prompt"),
        reply_markup=get_issue_keyboard(current_lang, i18n),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("server_report_host:"))
async def toggle_host_callback(callback: types.CallbackQuery, state: FSMContext, i18n_data: dict):
    current_lang = i18n_data.get("current_language", "ru")
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error", show_alert=True)
        return
    data = await state.get_data()
    hosts = list(data.get("server_report_hosts") or [])
    selected = set(int(i) for i in (data.get("server_report_selected") or []))
    try:
        idx = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer(i18n.gettext(current_lang, "error_try_again"), show_alert=True)
        return
    if idx < 0 or idx >= len(hosts):
        await callback.answer(i18n.gettext(current_lang, "error_try_again"), show_alert=True)
        return
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    await state.update_data(server_report_selected=sorted(selected))
    await callback.message.edit_reply_markup(reply_markup=get_hosts_keyboard(current_lang, i18n, hosts, selected))
    await callback.answer()


@router.callback_query(F.data == "server_report_submit")
async def submit_report_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    bot: Bot,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error", show_alert=True)
        return
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    last_report = await server_report_dal.get_last_report_by_user(session, callback.from_user.id)
    cooldown_until = report_cooldown_until(last_report, settings.SERVER_REPORT_COOLDOWN_HOURS)
    if cooldown_until:
        await callback.answer(_("server_report_cooldown_alert", time_left=_format_cooldown(cooldown_until)), show_alert=True)
        return

    data = await state.get_data()
    issue_code = data.get("server_report_issue")
    hosts = list(data.get("server_report_hosts") or [])
    selected = [int(i) for i in (data.get("server_report_selected") or [])]
    if not issue_code or issue_code not in ISSUE_TYPE_KEYS:
        await callback.answer(_("server_report_issue_required"), show_alert=True)
        return
    if not selected:
        await callback.answer(_("server_report_hosts_required"), show_alert=True)
        return

    selected_hosts = [hosts[idx] for idx in selected if 0 <= idx < len(hosts)]
    service = ServerReportService(settings=settings, panel_service=panel_service, bot=bot, i18n=i18n)
    await service.create_report(
        session,
        user_id=callback.from_user.id,
        issue_type=issue_code,
        selected_hosts=selected_hosts,
    )
    await state.clear()
    await callback.message.edit_text(
        _("server_report_thanks"),
        reply_markup=get_back_to_main_menu_markup(current_lang, i18n),
    )
    await callback.answer()

