import hashlib
import logging
import math
from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from typing import Optional, Union
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from config.settings import Settings
from bot.keyboards.inline.user_keyboards import (
    get_product_catalog_keyboard,
    get_offer_selection_keyboard,
    get_back_to_main_menu_markup,
    get_autorenew_confirm_keyboard,
)
from bot.utils.product_offers import (
    resolve_base_price,
)
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
from bot.middlewares.i18n import JsonI18n
from db.dal import subscription_dal, user_billing_dal
from db.models import Subscription
from bot.utils.product_kinds import (
    PAYMENT_KIND_BASE_SUBSCRIPTION,
    PAYMENT_KIND_COMBINED_SUBSCRIPTION,
    PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
    SUBSCRIPTION_KIND_ADDON,
    SUBSCRIPTION_KIND_BASE,
)

router = Router(name="user_subscription_core_router")


def _shorten_hwid_for_display(hwid: Optional[str], max_length: int = 24) -> str:
    """Trim HWID for button text to keep within Telegram limits."""
    if not hwid:
        return "-"
    hwid_str = str(hwid)
    if len(hwid_str) <= max_length:
        return hwid_str
    return f"{hwid_str[:8]}...{hwid_str[-6:]}"


def _hwid_callback_token(hwid: Optional[str]) -> str:
    """Stable short token for callback_data; avoids 64b limit with raw HWID."""
    hwid_str = str(hwid or "")
    return hashlib.sha256(hwid_str.encode()).hexdigest()[:32]


def _format_bytes_gb(value: Optional[float], fallback: str) -> str:
    if value is None:
        return fallback
    try:
        value_gb = float(value) / (2 ** 30)
        return f"{value_gb:.2f} GB"
    except Exception:
        return str(value)


def _addon_status_key(raw_status: Optional[str]) -> str:
    normalized = (raw_status or "").upper()
    if normalized in {"ACTIVE"}:
        return "addon_status_active"
    if normalized in {"SUSPENDED_BASE_REQUIRED", "SUSPENDED"}:
        return "addon_status_suspended"
    if normalized in {"TRAFFIC_EXHAUSTED", "LIMITED"}:
        return "addon_status_limited"
    if normalized in {"EXPIRED"}:
        return "addon_status_expired"
    return "addon_status_unknown"


async def display_subscription_options(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
    promo_code_service=None,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n:
        err_msg = "Language service error."
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(err_msg, show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        elif isinstance(event, types.Message):
            await event.answer(err_msg)
        return

    async def _build_display_options(raw_options, payment_kind: str):
        display: dict[float, tuple[float, str]] = {}
        active_discount_info = None
        if promo_code_service:
            try:
                active_discount_info = await promo_code_service.get_user_active_discount(
                    session,
                    event.from_user.id,
                    payment_kind=payment_kind,
                )
            except Exception:
                active_discount_info = None
        for raw_value in raw_options.keys():
            effective_value = float(raw_value)
            rub_price = resolve_base_price(settings, effective_value, payment_kind, stars=False)
            stars_price = resolve_base_price(settings, effective_value, payment_kind, stars=True)
            if active_discount_info:
                discount_pct, _promo_code = active_discount_info
                if rub_price is not None:
                    rub_price, _ = promo_code_service.calculate_discounted_price(rub_price, discount_pct)
                if stars_price is not None:
                    discounted_stars_price, _ = promo_code_service.calculate_discounted_price(float(stars_price), discount_pct)
                    stars_price = math.ceil(discounted_stars_price)
            if rub_price is not None:
                display[effective_value] = (rub_price, "RUB")
            elif stars_price is not None:
                display[effective_value] = (float(stars_price), "⭐")
        return display

    base_active_sub = await subscription_dal.get_active_subscription_by_user_id(
        session,
        event.from_user.id,
        kind=SUBSCRIPTION_KIND_BASE,
    )
    addon_active_sub = await subscription_dal.get_active_subscription_by_user_id(
        session,
        event.from_user.id,
        kind=SUBSCRIPTION_KIND_ADDON,
    )

    base_options = await _build_display_options(settings.subscription_options or {}, "base_subscription")
    combined_options = (
        await _build_display_options(settings.combined_subscription_options or {}, PAYMENT_KIND_COMBINED_SUBSCRIPTION)
        if settings.addon_enabled
        else {}
    )
    addon_upgrade_options = (
        await _build_display_options(settings.addon_subscription_options or {}, "addon_subscription")
        if base_active_sub and settings.addon_enabled
        else {}
    )
    addon_topups = (
        await _build_display_options(settings.addon_traffic_packages or {}, "addon_traffic_topup")
        if addon_active_sub
        else {}
    )

    if base_options or combined_options or addon_upgrade_options or addon_topups:
        text_content = get_text("select_subscription_catalog")
        reply_markup = get_product_catalog_keyboard(
            show_base_plan=bool(base_options),
            show_combined_plan=bool(combined_options),
            show_addon_upgrade=bool(addon_upgrade_options),
            show_addon_topup=bool(addon_topups),
            lang=current_lang,
            i18n_instance=i18n,
        )
    else:
        text_content = get_text("no_subscription_options_available")
        reply_markup = get_back_to_main_menu_markup(current_lang, i18n)

    target_message_obj = event.message if isinstance(event, types.CallbackQuery) else event
    if not target_message_obj:
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(get_text("error_occurred_try_again"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    if isinstance(event, types.CallbackQuery):
        try:
            await target_message_obj.edit_text(text_content, reply_markup=reply_markup)
        except Exception:
            await target_message_obj.answer(text_content, reply_markup=reply_markup)
        try:
            await event.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    else:
        await target_message_obj.answer(text_content, reply_markup=reply_markup)


@router.callback_query(F.data == "main_action:subscribe")
async def reshow_subscription_options_callback(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
    promo_code_service=None,
):
    await display_subscription_options(
        callback, i18n_data, settings, session, promo_code_service=promo_code_service
    )


@router.callback_query(F.data.startswith("subscription_catalog:"))
async def show_tariff_offer_options_callback(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,
    promo_code_service=None,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Error", show_alert=True)
        return
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    try:
        product_code = callback.data.split(":", 1)[1]
    except Exception:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    async def _build_display_options(raw_options, payment_kind: str):
        display: dict[float, tuple[float, str]] = {}
        active_discount_info = None
        if promo_code_service:
            try:
                active_discount_info = await promo_code_service.get_user_active_discount(
                    session,
                    callback.from_user.id,
                    payment_kind=payment_kind,
                )
            except Exception:
                active_discount_info = None
        for raw_value in raw_options.keys():
            effective_value = float(raw_value)
            rub_price = resolve_base_price(settings, effective_value, payment_kind, stars=False)
            stars_price = resolve_base_price(settings, effective_value, payment_kind, stars=True)
            if active_discount_info:
                discount_pct, _promo_code = active_discount_info
                if rub_price is not None:
                    rub_price, _ = promo_code_service.calculate_discounted_price(rub_price, discount_pct)
                if stars_price is not None:
                    discounted_stars_price, _ = promo_code_service.calculate_discounted_price(float(stars_price), discount_pct)
                    stars_price = math.ceil(discounted_stars_price)
            if rub_price is not None:
                display[effective_value] = (rub_price, "RUB")
            elif stars_price is not None:
                display[effective_value] = (float(stars_price), "⭐")
        return display

    if product_code == "base":
        offers = await _build_display_options(settings.subscription_options or {}, PAYMENT_KIND_BASE_SUBSCRIPTION)
        text_content = get_text("base_plan_offer_description")
        reply_markup = get_offer_selection_keyboard(
            offers=offers,
            callback_prefix="subscribe_period",
            lang=current_lang,
            i18n_instance=i18n,
            back_callback="main_action:subscribe",
        )
    elif product_code == "combined":
        offers = await _build_display_options(settings.combined_subscription_options or {}, PAYMENT_KIND_COMBINED_SUBSCRIPTION)
        text_content = get_text(
            "combined_plan_offer_description",
            monthly_traffic=f"{settings.ADDON_MONTHLY_TRAFFIC_GB:g}" if settings.ADDON_MONTHLY_TRAFFIC_GB else "0",
        )
        reply_markup = get_offer_selection_keyboard(
            offers=offers,
            callback_prefix="subscribe_combined_period",
            lang=current_lang,
            i18n_instance=i18n,
            back_callback="main_action:subscribe",
        )
    elif product_code == "addon_upgrade":
        offers = await _build_display_options(settings.addon_subscription_options or {}, "addon_subscription")
        text_content = get_text("addon_upgrade_offer_description")
        reply_markup = get_offer_selection_keyboard(
            offers=offers,
            callback_prefix="subscribe_addon_period",
            lang=current_lang,
            i18n_instance=i18n,
            back_callback="main_action:subscribe",
        )
    elif product_code == "addon_topup":
        offers = await _build_display_options(settings.addon_traffic_packages or {}, PAYMENT_KIND_ADDON_TRAFFIC_TOPUP)
        text_content = get_text("addon_topup_offer_description")
        reply_markup = get_offer_selection_keyboard(
            offers=offers,
            callback_prefix="subscribe_addon_traffic",
            lang=current_lang,
            i18n_instance=i18n,
            traffic_mode=True,
            back_callback="main_action:subscribe",
        )
    else:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    try:
        await callback.message.edit_text(text_content, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text_content, reply_markup=reply_markup)
    await callback.answer()


async def my_subscription_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot,
):
    target = event.message if isinstance(event, types.CallbackQuery) else event
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    if not i18n or not target:
        if isinstance(event, types.Message):
            await event.answer(get_text("error_occurred_try_again"))
        return

    if not panel_service or not subscription_service:
        await target.answer(get_text("error_service_unavailable"))
        return

    overview = await subscription_service.get_subscription_overview(session, event.from_user.id)
    base_active = overview.get("base")
    addon_active = overview.get("addon")

    if not base_active and not addon_active:
        text = get_text("subscription_not_active")

        buy_button = InlineKeyboardButton(
            text=get_text("menu_subscribe_inline"), callback_data="main_action:subscribe"
        )
        back_markup = get_back_to_main_menu_markup(current_lang, i18n)

        kb = InlineKeyboardMarkup(inline_keyboard=[[buy_button], *back_markup.inline_keyboard])

        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer()
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            try:
                await event.message.edit_text(text, reply_markup=kb)
            except Exception:
                await event.message.answer(text, reply_markup=kb)
        else:
            await event.answer(text, reply_markup=kb)
        return

    base_config_link_display = base_active.get("config_link") if base_active else None
    base_connect_button_url = base_active.get("connect_button_url") if base_active else None
    addon_config_link_display = addon_active.get("config_link") if addon_active else None
    addon_connect_button_url = addon_active.get("connect_button_url") if addon_active else None

    text_parts = []
    if base_active:
        end_date = base_active.get("end_date")
        days_left = (end_date.date() - datetime.now().date()).days if end_date else 0
        text_parts.append(
            get_text(
                "my_subscription_base_block",
                end_date=end_date.strftime("%Y-%m-%d") if end_date else "N/A",
                days_left=max(0, days_left),
                status=base_active.get("status_from_panel", get_text("status_active")).capitalize(),
                config_link=base_config_link_display or get_text("config_link_not_available"),
                traffic_limit=(
                    _format_bytes_gb(base_active.get("traffic_limit_bytes"), get_text("traffic_unlimited"))
                    if base_active.get("traffic_limit_bytes")
                    else get_text("traffic_unlimited")
                ),
                traffic_used=_format_bytes_gb(base_active.get("traffic_used_bytes"), get_text("traffic_na")),
            )
        )
    else:
        text_parts.append(get_text("my_subscription_base_missing"))

    if addon_active:
        addon_end_date = addon_active.get("end_date")
        text_parts.append(
            get_text(
                "my_subscription_addon_block",
                status=get_text(_addon_status_key(addon_active.get("addon_state") or addon_active.get("status_from_panel"))),
                end_date=addon_end_date.strftime("%Y-%m-%d") if addon_end_date else "N/A",
                included_remaining=_format_bytes_gb(
                    addon_active.get("included_traffic_remaining_bytes"),
                    get_text("traffic_na"),
                ),
                topup_remaining=_format_bytes_gb(
                    addon_active.get("addon_topup_remaining_bytes"),
                    get_text("traffic_na"),
                ),
                total_remaining=_format_bytes_gb(
                    addon_active.get("traffic_remaining_bytes"),
                    get_text("traffic_na"),
                ),
                config_link=addon_config_link_display or get_text("config_link_not_available"),
            )
        )
    elif settings.addon_enabled:
        text_parts.append(get_text("my_subscription_addon_missing"))

    text = "\n\n".join(text_parts)

    base_markup = get_back_to_main_menu_markup(current_lang, i18n)
    kb = base_markup.inline_keyboard
    try:
        local_sub = await subscription_dal.get_active_subscription_by_user_id(
            session,
            event.from_user.id,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        # Build rows to prepend above the base "back" markup
        prepend_rows = []

        if settings.SUBSCRIPTION_MINI_APP_URL and base_active:
            prepend_rows.append([
                InlineKeyboardButton(
                    text=get_text("connect_base_button"),
                    web_app=WebAppInfo(url=settings.SUBSCRIPTION_MINI_APP_URL),
                )
            ])
        elif base_active:
            cfg_link_val = base_connect_button_url or base_config_link_display
            if cfg_link_val:
                prepend_rows.append([
                    InlineKeyboardButton(
                        text=get_text("connect_base_button"),
                        url=cfg_link_val,
                    )
                ])

        if addon_active:
            addon_link_val = addon_connect_button_url or addon_config_link_display
            if addon_link_val:
                prepend_rows.append([
                    InlineKeyboardButton(
                        text=get_text("connect_addon_button"),
                        url=addon_link_val,
                    )
                ])

        if settings.MY_DEVICES_SECTION_ENABLED and base_active:
            max_devices_value = base_active.get("max_devices")
            max_devices_display = get_text("devices_unlimited_label")
            if max_devices_value not in (None, 0):
                try:
                    max_devices_int = int(max_devices_value)
                    if max_devices_int >= 0:
                        max_devices_display = str(max_devices_int)
                except (TypeError, ValueError):
                    max_devices_display = str(max_devices_value)
            current_devices_display = "?"
            user_uuid = base_active.get("user_id")
            devices_response = None
            if user_uuid:
                try:
                    devices_response = await panel_service.get_user_devices(user_uuid)
                except Exception:
                    logging.exception("Failed to load devices for user %s", user_uuid)
            if devices_response:
                devices_count: Optional[int] = None
                if isinstance(devices_response, dict):
                    devices_list = devices_response.get("devices")
                    if isinstance(devices_list, list):
                        devices_count = len(devices_list)
                    elif isinstance(devices_list, int):
                        devices_count = devices_list
                    else:
                        try:
                            devices_count = len(devices_list)  # type: ignore[arg-type]
                        except Exception:
                            devices_count = None
                    if devices_count is None:
                        total_value = devices_response.get("total")
                        if isinstance(total_value, int):
                            devices_count = total_value
                elif isinstance(devices_response, list):
                    devices_count = len(devices_response)
                if devices_count is not None:
                    current_devices_display = str(devices_count)
            devices_button_text = get_text(
                "devices_button",
                current_devices=current_devices_display,
                max_devices=max_devices_display,
            )
            prepend_rows.append([
                InlineKeyboardButton(
                    text=devices_button_text,
                    callback_data="main_action:my_devices",
                )
            ])

        # 2) Auto-renew toggle (YooKassa only)
        if local_sub and local_sub.provider == "yookassa" and settings.yookassa_autopayments_active:
            toggle_text = (
                get_text("autorenew_disable_button") if local_sub.auto_renew_enabled else get_text("autorenew_enable_button")
            )
            prepend_rows.append([
                InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"toggle_autorenew:{local_sub.subscription_id}:{1 if not local_sub.auto_renew_enabled else 0}",
                )
            ])

        # 3) Payment methods management (when autopayments enabled)
        if base_active and settings.yookassa_autopayments_active:
            prepend_rows.append([
                InlineKeyboardButton(text=get_text("payment_methods_manage_button"), callback_data="pm:manage")
            ])

        if prepend_rows:
            kb = prepend_rows + kb
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        try:
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            await bot.send_message(
                chat_id=target.chat.id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data == "main_action:my_devices")
async def my_devices_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot,
):
    target = event.message if isinstance(event, types.CallbackQuery) else event
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    if not i18n or not target:
        if isinstance(event, types.Message):
            await event.answer(get_text("error_occurred_try_again"))
        return

    if not settings.MY_DEVICES_SECTION_ENABLED:
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(get_text("my_devices_feature_disabled"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        else:
            await target.answer(get_text("my_devices_feature_disabled"))
        return

    # TODO: context?
    active = await subscription_service.get_active_subscription_details(session, event.from_user.id)
    if not active or not active.get("user_id"):
        message = get_text("subscription_not_active")
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(message, show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        else:
            await target.answer(message)
        return

    devices = await panel_service.get_user_devices(active.get("user_id")) if active else None
    if not devices:
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(get_text("no_devices_found"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        else:
            await target.answer(get_text("no_devices_found"))
        return

    devices_list_raw = []
    if isinstance(devices, dict):
        devices_list_raw = devices.get("devices") or []
    elif isinstance(devices, list):
        devices_list_raw = devices

    max_devices_value = active.get("max_devices")
    max_devices_display = get_text("devices_unlimited_label")
    if max_devices_value not in (None, 0):
        try:
            max_devices_int = int(max_devices_value)
            if max_devices_int >= 0:
                max_devices_display = str(max_devices_int)
        except (TypeError, ValueError):
            max_devices_display = str(max_devices_value)

    if not devices_list_raw:
        text = get_text("no_devices_details_found_message", max_devices=max_devices_display)
    else:
        devices_list = []
        current_devices = len(devices_list_raw)
        for index, device in enumerate(devices_list_raw, start=1):
            device_model = device.get('deviceModel') or None
            platform = device.get('platform') or None
            user_agent = device.get('userAgent') or None
            os_version = device.get('osVersion') or None
            created_at = device.get('createdAt')
            hwid = device.get('hwid')
            try:
                created_at_str = datetime.fromisoformat(created_at).strftime("%d.%m.%Y %H:%M") if created_at else "-"
            except Exception:
                created_at_str = str(created_at)

            device_details = get_text("device_details", index=index, device_model=device_model, platform=platform, os_version=os_version, created_at_str=created_at_str, user_agent=user_agent, hwid=hwid)
            devices_list.append(device_details)

        text = get_text("my_devices_details", devices="\n\n".join(devices_list), current_devices=current_devices, max_devices=max_devices_display)

    base_markup = get_back_to_main_menu_markup(current_lang, i18n, callback_data="main_action:my_subscription")
    kb = base_markup.inline_keyboard

    devices_kb = []
    for index, device in enumerate(devices_list_raw, start=1):
        hwid = device.get('hwid')
        if not hwid:
            continue
        device_button_text = get_text("disconnect_device_button", hwid=_shorten_hwid_for_display(hwid), index=index)
        hwid_token = _hwid_callback_token(hwid)

        devices_kb.append([InlineKeyboardButton(text=device_button_text, callback_data=f"disconnect_device:{hwid_token}")])
    kb = devices_kb + kb
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    if isinstance(event, types.CallbackQuery):
        try:
            await event.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except Exception:
            await event.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("disconnect_device:"))
async def disconnect_device_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not settings.MY_DEVICES_SECTION_ENABLED:
        try:
            await callback.answer(get_text("my_devices_feature_disabled"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    try:
        _, hwid_token = callback.data.split(":", 1)
    except Exception:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    active = await subscription_service.get_active_subscription_details(session, callback.from_user.id)
    if not active or not active.get("user_id"):
        await callback.answer(get_text("subscription_not_active"), show_alert=True)
        return

    devices = await panel_service.get_user_devices(active.get("user_id"))
    if not devices:
        await callback.answer(get_text("no_devices_found"), show_alert=True)
        return

    devices_list_raw = []
    if isinstance(devices, dict):
        devices_list_raw = devices.get("devices") or []
    elif isinstance(devices, list):
        devices_list_raw = devices

    hwid = None
    for device in devices_list_raw:
        hwid_candidate = device.get("hwid")
        if hwid_candidate and _hwid_callback_token(hwid_candidate) == hwid_token:
            hwid = hwid_candidate
            break

    if not hwid:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    success = await panel_service.disconnect_device(active.get("user_id"), hwid)
    if not success:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    await session.commit()
    try:
        await callback.answer(get_text("device_disconnected"))
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    await my_devices_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)


@router.callback_query(F.data.startswith("toggle_autorenew:"))
async def toggle_autorenew_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    try:
        _, payload = callback.data.split(":", 1)
        sub_id_str, enable_str = payload.split(":")
        sub_id = int(sub_id_str)
        enable = bool(int(enable_str))
    except Exception:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    sub = await session.get(Subscription, sub_id)
    if not sub or sub.user_id != callback.from_user.id:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if sub.provider != "yookassa":
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if enable:
        has_saved_card = await user_billing_dal.user_has_saved_payment_method(session, callback.from_user.id)
        if not has_saved_card:
            try:
                await callback.answer(get_text("autorenew_enable_requires_card"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            return

    # Show confirmation popup and inline buttons
    confirm_text = get_text("autorenew_confirm_enable") if enable else get_text("autorenew_confirm_disable")
    kb = get_autorenew_confirm_keyboard(enable, sub.subscription_id, current_lang, i18n)
    try:
        await callback.message.edit_text(confirm_text, reply_markup=kb)
    except Exception:
        try:
            await callback.message.answer(confirm_text, reply_markup=kb)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    try:
        await callback.answer()
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    return


@router.callback_query(F.data.startswith("autorenew:confirm:"))
async def confirm_autorenew_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    try:
        _, _, sub_id_str, enable_str = callback.data.split(":", 3)
        sub_id = int(sub_id_str)
        enable = bool(int(enable_str))
    except Exception:
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return

    sub = await session.get(Subscription, sub_id)
    if not sub or sub.user_id != callback.from_user.id:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if sub.provider != "yookassa":
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return
    if enable:
        has_saved_card = await user_billing_dal.user_has_saved_payment_method(session, callback.from_user.id)
        if not has_saved_card:
            try:
                await callback.answer(get_text("autorenew_enable_requires_card"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            try:
                await my_subscription_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
            return

    await subscription_dal.update_subscription(session, sub.subscription_id, {"auto_renew_enabled": enable})
    await session.commit()
    try:
        await callback.answer(get_text("subscription_autorenew_updated"))
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    await my_subscription_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)


@router.callback_query(F.data == "autorenew:cancel")
async def autorenew_cancel_from_webhook_button(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    # Disable auto-renew on the active subscription
    from db.dal import subscription_dal
    sub = await subscription_dal.get_active_subscription_by_user_id(session, callback.from_user.id)
    if not sub:
        try:
            await callback.answer(get_text("subscription_not_active"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return
    if sub.provider != "yookassa":
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
        return
    await subscription_dal.update_subscription(session, sub.subscription_id, {"auto_renew_enabled": False})
    await session.commit()
    try:
        await callback.answer(get_text("subscription_autorenew_updated"))
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/core.py: %s", exc)
    await my_subscription_command_handler(callback, i18n_data, settings, panel_service, subscription_service, session, bot)


@router.message(Command("connect"))
async def connect_command_handler(
    message: types.Message,
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot,
):
    logging.info(f"User {message.from_user.id} used /connect command.")
    await my_subscription_command_handler(message, i18n_data, settings, panel_service, subscription_service, session, bot)
