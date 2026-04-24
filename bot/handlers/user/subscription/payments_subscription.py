import logging
import math
from typing import Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.utils.product_offers import (
    get_fiat_price_source,
    get_stars_price_source,
    is_traffic_payment_kind,
    normalize_payment_kind,
    resolve_base_price,
)
from bot.keyboards.inline.user_keyboards import get_payment_method_keyboard
from bot.middlewares.i18n import JsonI18n
from config.settings import Settings
from db.dal import subscription_dal
from bot.utils.product_kinds import SUBSCRIPTION_KIND_ADDON, SUBSCRIPTION_KIND_BASE
from bot.utils.product_kinds import PAYMENT_KIND_COMBINED_SUBSCRIPTION

router = Router(name="user_subscription_payments_selection_router")


async def resolve_fiat_offer_price_for_user(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    value: Optional[float] = None,
    payment_kind: Optional[str] = None,
    promo_code_service=None,
    months: Optional[float] = None,
    sale_mode: Optional[str] = None,
) -> Optional[float]:
    """Resolve offer price server-side to prevent callback payload tampering."""
    if value is None:
        value = months
    if payment_kind is None:
        payment_kind = sale_mode
    if value is None or payment_kind is None:
        return None
    payment_kind = normalize_payment_kind(payment_kind)
    base_price = resolve_base_price(settings, value, payment_kind, stars=False)
    if base_price is None:
        return None

    resolved_price = float(base_price)
    if promo_code_service:
        active_discount_info = await promo_code_service.get_user_active_discount(
            session,
            user_id,
            payment_kind=payment_kind,
        )
        if active_discount_info:
            discount_pct, _, max_discount_amount, combined_discount_scope = active_discount_info
            offer_details = promo_code_service.calculate_discounted_offer_details(
                value=value,
                payment_kind=payment_kind,
                discount_percentage=discount_pct,
                max_discount_amount=max_discount_amount,
                combined_discount_scope=combined_discount_scope,
            )
            if offer_details:
                resolved_price = float(offer_details["final_price"])
    return resolved_price


def _build_discount_details_text(
    get_text,
    *,
    payment_kind: str,
    combined_discount_scope: str,
    cap_applied: bool,
    max_discount_amount: Optional[float],
    currency_symbol: str,
) -> str:
    details: list[str] = []
    if (
        payment_kind == PAYMENT_KIND_COMBINED_SUBSCRIPTION
        and combined_discount_scope == "base_only"
    ):
        details.append(get_text("active_discount_detail_combined_base_only"))
    if cap_applied and max_discount_amount is not None:
        details.append(
            get_text(
                "active_discount_detail_max_discount",
                amount=f"{float(max_discount_amount):g}",
                currency_symbol=currency_symbol,
            )
        )
    return "\n".join(details)


async def _render_payment_method_selection(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    *,
    raw_value: float,
    payment_kind: str,
    promo_code_service=None,
) -> None:
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
        return

    payment_kind = normalize_payment_kind(payment_kind)
    base_sub = await subscription_dal.get_active_subscription_by_user_id(
        session,
        callback.from_user.id,
        kind=SUBSCRIPTION_KIND_BASE,
    )
    addon_sub = await subscription_dal.get_active_subscription_by_user_id(
        session,
        callback.from_user.id,
        kind=SUBSCRIPTION_KIND_ADDON,
    )
    if payment_kind == "addon_subscription" and not base_sub:
        try:
            await callback.answer(get_text("addon_requires_base_subscription"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
        return
    if payment_kind == "addon_traffic_topup" and not addon_sub:
        try:
            await callback.answer(get_text("addon_topup_requires_addon"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
        return

    display_value = 1.0 if payment_kind == "addon_subscription" else float(raw_value)
    price_rub = resolve_base_price(settings, display_value, payment_kind, stars=False)
    stars_price = resolve_base_price(settings, display_value, payment_kind, stars=True)
    currency_symbol_val = "RUB"

    discount_text = ""
    if promo_code_service and (price_rub is not None or stars_price is not None):
        active_discount_info = await promo_code_service.get_user_active_discount(
            session,
            callback.from_user.id,
            payment_kind=payment_kind,
        )

        if active_discount_info:
            discount_pct, promo_code, max_discount_amount, combined_discount_scope = active_discount_info
            if price_rub is not None:
                price_details_rub = promo_code_service.calculate_discounted_offer_details(
                    value=display_value,
                    payment_kind=payment_kind,
                    discount_percentage=discount_pct,
                    max_discount_amount=max_discount_amount,
                    combined_discount_scope=combined_discount_scope,
                )
                original_price_rub = float(price_details_rub["original_price"]) if price_details_rub else float(price_rub)
                price_rub = float(price_details_rub["final_price"]) if price_details_rub else float(price_rub)
                discount_amt = float(price_details_rub["discount_amount"]) if price_details_rub else 0.0
                details_text = _build_discount_details_text(
                    get_text,
                    payment_kind=payment_kind,
                    combined_discount_scope=combined_discount_scope,
                    cap_applied=bool(price_details_rub["cap_applied"]) if price_details_rub else False,
                    max_discount_amount=max_discount_amount,
                    currency_symbol=currency_symbol_val,
                )
                discount_text = get_text(
                    "active_discount_notice",
                    code=promo_code,
                    discount_pct=discount_pct,
                    original_price=original_price_rub,
                    discounted_price=price_rub,
                    discount_amount=discount_amt,
                    currency_symbol=currency_symbol_val,
                    details=(f"\n{details_text}" if details_text else ""),
                )
            if stars_price is not None:
                price_details_stars = promo_code_service.calculate_discounted_offer_details(
                    value=display_value,
                    payment_kind=payment_kind,
                    discount_percentage=discount_pct,
                    max_discount_amount=max_discount_amount,
                    combined_discount_scope=combined_discount_scope,
                    stars=True,
                )
                original_stars_price = float(price_details_stars["original_price"]) if price_details_stars else float(stars_price)
                discounted_stars_price = float(price_details_stars["final_price"]) if price_details_stars else float(stars_price)
                discounted_stars_price = math.ceil(discounted_stars_price)
                stars_price = discounted_stars_price
                if not discount_text:
                    discount_amt = original_stars_price - discounted_stars_price
                    details_text = _build_discount_details_text(
                        get_text,
                        payment_kind=payment_kind,
                        combined_discount_scope=combined_discount_scope,
                        cap_applied=bool(price_details_stars["cap_applied"]) if price_details_stars else False,
                        max_discount_amount=max_discount_amount,
                        currency_symbol="⭐",
                    )
                    discount_text = get_text(
                        "active_discount_notice",
                        code=promo_code,
                        discount_pct=discount_pct,
                        original_price=original_stars_price,
                        discounted_price=discounted_stars_price,
                        discount_amount=discount_amt,
                        currency_symbol="⭐",
                        details=(f"\n{details_text}" if details_text else ""),
                    )

    if price_rub is None:
        if price_rub is None and stars_price is not None:
            currency_methods_enabled = any(
                [
                    settings.FREEKASSA_ENABLED,
                    settings.PLATEGA_ENABLED,
                    settings.SEVERPAY_ENABLED,
                    settings.YOOKASSA_ENABLED,
                    settings.CRYPTOPAY_ENABLED,
                ]
            )
            if currency_methods_enabled:
                logging.error(
                    "Currency price missing for payment kind %s option %s while fiat providers are enabled.",
                    payment_kind,
                    display_value,
                )
                try:
                    await callback.answer(get_text("error_try_again"), show_alert=True)
                except Exception as exc:
                    logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
                return
            price_rub = 0.0
            currency_symbol_val = "⭐"
        else:
            try:
                await callback.answer(get_text("error_try_again"), show_alert=True)
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)
            return

    if payment_kind == PAYMENT_KIND_COMBINED_SUBSCRIPTION:
        text_content = get_text("choose_payment_method_combined")
    elif payment_kind == "addon_subscription":
        text_content = get_text("choose_payment_method_addon")
    elif is_traffic_payment_kind(payment_kind):
        text_content = get_text("choose_payment_method_traffic")
    else:
        text_content = get_text("choose_payment_method")
    if discount_text:
        text_content = f"{discount_text}\n\n{text_content}"

    reply_markup = get_payment_method_keyboard(
        display_value,
        price_rub,
        stars_price,
        currency_symbol_val,
        current_lang,
        i18n,
        settings,
        sale_mode=payment_kind,
    )

    try:
        await callback.message.edit_text(text_content, reply_markup=reply_markup)
    except Exception as e_edit:
        logging.warning(
            f"Edit message for payment method selection failed: {e_edit}. Sending new one."
        )
        await callback.message.answer(text_content, reply_markup=reply_markup)
    try:
        await callback.answer()
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_subscription.py: %s", exc)


@router.callback_query(F.data.startswith("subscribe_period:"))
async def select_base_subscription_period_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    promo_code_service=None,
):
    try:
        raw_value = float(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback.answer("Error", show_alert=True)
        return
    await _render_payment_method_selection(
        callback,
        settings,
        i18n_data,
        session,
        raw_value=raw_value,
        payment_kind="base_subscription",
        promo_code_service=promo_code_service,
    )


@router.callback_query(F.data.startswith("subscribe_addon_period:"))
async def select_addon_subscription_period_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    promo_code_service=None,
):
    try:
        raw_value = float(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback.answer("Error", show_alert=True)
        return
    await _render_payment_method_selection(
        callback,
        settings,
        i18n_data,
        session,
        raw_value=raw_value,
        payment_kind="addon_subscription",
        promo_code_service=promo_code_service,
    )


@router.callback_query(F.data.startswith("subscribe_combined_period:"))
async def select_combined_subscription_period_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    promo_code_service=None,
):
    try:
        raw_value = float(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback.answer("Error", show_alert=True)
        return
    await _render_payment_method_selection(
        callback,
        settings,
        i18n_data,
        session,
        raw_value=raw_value,
        payment_kind="combined_subscription",
        promo_code_service=promo_code_service,
    )


@router.callback_query(F.data.startswith("subscribe_addon_traffic:"))
async def select_addon_traffic_package_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    promo_code_service=None,
):
    try:
        raw_value = float(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback.answer("Error", show_alert=True)
        return
    await _render_payment_method_selection(
        callback,
        settings,
        i18n_data,
        session,
        raw_value=raw_value,
        payment_kind="addon_traffic_topup",
        promo_code_service=promo_code_service,
    )
