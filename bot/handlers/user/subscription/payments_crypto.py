import logging
from typing import Optional

from aiogram import F, Router, types
from sqlalchemy.ext.asyncio import AsyncSession

from bot.utils.product_offers import (
    get_payment_description,
    get_payment_link_message_key,
    normalize_payment_kind,
)
from bot.keyboards.inline.user_keyboards import get_payment_url_keyboard
from bot.middlewares.i18n import JsonI18n
from bot.services.crypto_pay_service import CryptoPayService
from config.settings import Settings

router = Router(name="user_subscription_payments_crypto_router")


from bot.handlers.user.subscription.payments_subscription import resolve_fiat_offer_price_for_user


def _get_back_offer_callback(value: float, payment_kind: str) -> str:
    value_str = str(int(value)) if float(value).is_integer() else f"{value:g}"
    payment_kind = normalize_payment_kind(payment_kind)
    if payment_kind == "addon_subscription":
        return f"subscribe_addon_period:{value_str}"
    if payment_kind == "addon_traffic_topup":
        return f"subscribe_addon_traffic:{value_str}"
    return f"subscribe_period:{value_str}"

@router.callback_query(F.data.startswith("pay_crypto:"))
async def pay_crypto_callback_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    cryptopay_service: CryptoPayService,
    promo_code_service=None,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key)

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
        return

    if not cryptopay_service or not getattr(cryptopay_service, "configured", False):
        try:
            await callback.answer(get_text("payment_service_unavailable_alert"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
        return

    try:
        _, data_payload = callback.data.split(":", 1)
        parts = data_payload.split(":")
        months = float(parts[0])
        callback_price_amount = float(parts[1])
        sale_mode = normalize_payment_kind(parts[2] if len(parts) > 2 else "base_subscription")
    except (ValueError, IndexError):
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
        return

    user_id = callback.from_user.id
    resolved_price_amount = await resolve_fiat_offer_price_for_user(
        session=session,
        settings=settings,
        user_id=user_id,
        value=months,
        payment_kind=sale_mode,
        promo_code_service=promo_code_service,
    )
    if resolved_price_amount is None:
        logging.warning(
            "CryptoPay: no server-side price for user %s, value=%s, mode=%s",
            user_id,
            months,
            sale_mode,
        )
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
        return

    if abs(resolved_price_amount - callback_price_amount) > 0.01:
        logging.warning(
            "CryptoPay: callback price mismatch for user %s, value=%s, mode=%s, callback=%.2f, resolved=%.2f",
            user_id,
            months,
            sale_mode,
            callback_price_amount,
            resolved_price_amount,
        )
        try:
            await callback.answer(get_text("error_try_again"), show_alert=True)
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
        return

    price_amount = resolved_price_amount
    human_value = str(int(months)) if float(months).is_integer() else f"{months:g}"
    payment_description = get_payment_description(get_text, months, sale_mode)

    invoice_url = await cryptopay_service.create_invoice(
        session=session,
        user_id=user_id,
        months=months,
        amount=price_amount,
        description=payment_description,
        sale_mode=sale_mode,
        promo_code_service=promo_code_service,
    )

    if invoice_url:
        try:
            await callback.message.edit_text(
                get_text(
                    key=get_payment_link_message_key(sale_mode),
                    months=int(months),
                    traffic_gb=human_value,
                ),
                reply_markup=get_payment_url_keyboard(
                    invoice_url,
                    current_lang,
                    i18n,
                    back_callback=_get_back_offer_callback(months, sale_mode),
                    back_text_key="back_to_payment_methods_button",
                ),
                disable_web_page_preview=False,
            )
        except Exception:
            try:
                await callback.message.answer(
                    get_text(
                        key=get_payment_link_message_key(sale_mode),
                        months=int(months),
                        traffic_gb=human_value,
                    ),
                    reply_markup=get_payment_url_keyboard(
                        invoice_url,
                        current_lang,
                        i18n,
                        back_callback=_get_back_offer_callback(months, sale_mode),
                        back_text_key="back_to_payment_methods_button",
                    ),
                    disable_web_page_preview=False,
                )
            except Exception as exc:
                logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
        try:
            await callback.answer()
        except Exception as exc:
            logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
        return

    try:
        await callback.answer(get_text("error_payment_gateway"), show_alert=True)
    except Exception as exc:
        logging.debug("Suppressed exception in bot/handlers/user/subscription/payments_crypto.py: %s", exc)
