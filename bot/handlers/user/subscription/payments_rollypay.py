import logging
from typing import Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.inline.user_keyboards import get_payment_url_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.rollypay_service import RollyPayService
from bot.utils.product_offers import (
    get_payment_description,
    get_payment_link_message_key,
    normalize_payment_kind,
)
from config.settings import Settings
from db.dal import payment_dal

from bot.handlers.user.subscription.payments_subscription import resolve_fiat_offer_price_for_user

router = Router(name="user_subscription_payments_rollypay_router")


def _get_back_offer_callback(value: float, payment_kind: str) -> str:
    value_str = str(int(value)) if float(value).is_integer() else f"{value:g}"
    payment_kind = normalize_payment_kind(payment_kind)
    if payment_kind == "combined_subscription":
        return f"subscribe_combined_period:{value_str}"
    if payment_kind == "addon_subscription":
        return f"subscribe_addon_period:{value_str}"
    if payment_kind == "addon_traffic_topup":
        return f"subscribe_addon_traffic:{value_str}"
    return f"subscribe_period:{value_str}"


@router.callback_query(F.data.startswith("pay_rollypay:"))
async def pay_rollypay_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    rollypay_service: RollyPayService,
    session: AsyncSession,
    promo_code_service=None,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
        return

    if not rollypay_service or not rollypay_service.configured:
        logging.error("RollyPay service is not configured or unavailable.")
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
        try:
            await callback.message.edit_text(get_text("payment_service_unavailable"))
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        parts = data_payload.split(":")
        months = float(parts[0])
        callback_price_rub = float(parts[1])
        sale_mode = normalize_payment_kind(parts[2] if len(parts) > 2 else "base_subscription")
    except (ValueError, IndexError):
        logging.error("Invalid pay_rollypay data in callback: %s", callback.data)
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
        return

    user_id = callback.from_user.id
    resolved_price_rub = await resolve_fiat_offer_price_for_user(
        session=session,
        settings=settings,
        user_id=user_id,
        value=months,
        payment_kind=sale_mode,
        promo_code_service=promo_code_service,
    )
    if resolved_price_rub is None:
        logging.warning(
            "RollyPay: no server-side price for user %s, value=%s, mode=%s",
            user_id,
            months,
            sale_mode,
        )
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
        return

    if abs(resolved_price_rub - callback_price_rub) > 0.01:
        logging.warning(
            "RollyPay: callback price mismatch for user %s, value=%s, mode=%s, callback=%.2f, resolved=%.2f",
            user_id,
            months,
            sale_mode,
            callback_price_rub,
            resolved_price_rub,
        )
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
        return

    price_rub = resolved_price_rub
    human_value = str(int(months)) if float(months).is_integer() else f"{months:g}"
    payment_description = get_payment_description(get_text, months, sale_mode)
    currency_code = "RUB"

    payment_record_payload = {
        "user_id": user_id,
        "amount": price_rub,
        "original_amount": None,
        "discount_applied": None,
        "currency": currency_code,
        "status": "pending_rollypay",
        "description": payment_description,
        "subscription_duration_months": int(months),
        "provider": "rollypay",
        "promo_code_id": None,
        "kind": sale_mode,
    }

    try:
        payment_record = await payment_dal.create_payment_record(session, payment_record_payload)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.error(
            "RollyPay: failed to create payment record for user %s: %s",
            user_id,
            exc,
            exc_info=True,
        )
        try:
            await callback.message.edit_text(get_text("error_creating_payment_record"))
        except Exception as edit_exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", edit_exc)
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as answer_exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", answer_exc)
        return

    success, response_data = await rollypay_service.create_payment(
        payment_db_id=payment_record.payment_id,
        user_id=user_id,
        months=months,
        amount=price_rub,
        currency=currency_code,
        description=payment_description,
        payment_kind=sale_mode,
        promo_code_service=promo_code_service,
        session=session,
    )

    if success:
        provider_payment_id = response_data.get("payment_id")
        payment_link = response_data.get("pay_url")
        provider_status_raw = str(response_data.get("status") or "").strip().lower()
        provider_status = "pending_rollypay" if provider_status_raw in {"", "created", "processing"} else provider_status_raw

        if provider_payment_id:
            try:
                await payment_dal.update_provider_payment_and_status(
                    session,
                    payment_record.payment_id,
                    str(provider_payment_id),
                    str(provider_status),
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logging.error(
                    "RollyPay: failed to store provider payment id for payment %s: %s",
                    payment_record.payment_id,
                    exc,
                    exc_info=True,
                )

        if payment_link:
            try:
                await callback.message.edit_text(
                    get_text(
                        key=get_payment_link_message_key(sale_mode),
                        months=int(months),
                        traffic_gb=human_value,
                    ),
                    reply_markup=get_payment_url_keyboard(
                        payment_link,
                        current_lang,
                        i18n,
                        back_callback=_get_back_offer_callback(months, sale_mode),
                        back_text_key="back_to_payment_methods_button",
                    ),
                    disable_web_page_preview=False,
                )
            except Exception as exc:
                logging.warning(
                    "RollyPay: failed to display payment link (%s), sending new message.",
                    exc,
                )
                try:
                    await callback.message.answer(
                        get_text(
                            key=get_payment_link_message_key(sale_mode),
                            months=int(months),
                            traffic_gb=human_value,
                        ),
                        reply_markup=get_payment_url_keyboard(
                            payment_link,
                            current_lang,
                            i18n,
                            back_callback=_get_back_offer_callback(months, sale_mode),
                            back_text_key="back_to_payment_methods_button",
                        ),
                        disable_web_page_preview=False,
                    )
                except Exception as answer_exc:
                    logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", answer_exc)
            try:
                await callback.answer()
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
            return

        logging.error(
            "RollyPay: payment created but missing pay_url for payment %s. Response: %s",
            payment_record.payment_id,
            response_data,
        )

    try:
        await payment_dal.update_payment_status_by_db_id(
            session,
            payment_record.payment_id,
            "failed_creation",
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logging.error(
            "RollyPay: failed to mark payment %s as failed_creation: %s",
            payment_record.payment_id,
            exc,
            exc_info=True,
        )

    try:
        await callback.message.edit_text(get_text("error_payment_gateway"))
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
    try:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_rollypay.py: %s", exc)
