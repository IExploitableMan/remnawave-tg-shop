import hashlib
import hmac
import json
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.services.subscription_service import SubscriptionService
from bot.services.referral_service import ReferralService
from bot.services.notification_service import NotificationService
from bot.keyboards.inline.user_keyboards import (
    get_channel_subscription_keyboard,
    get_connect_and_main_keyboard,
)
from bot.utils.product_offers import is_traffic_payment_kind, normalize_payment_kind
from bot.utils.product_kinds import (
    PAYMENT_KIND_ADDON_SUBSCRIPTION,
    PAYMENT_KIND_BASE_SUBSCRIPTION,
)
from db.dal import payment_dal, user_dal
from bot.utils.text_sanitizer import sanitize_display_name, username_for_display
from bot.utils.paid_link_gate import prepare_paid_config_links


class RollyPayService:
    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        i18n: JsonI18n,
        async_session_factory: sessionmaker,
        subscription_service: SubscriptionService,
        referral_service: ReferralService,
        default_return_url: str,
    ):
        self.bot = bot
        self.settings = settings
        self.i18n = i18n
        self.async_session_factory = async_session_factory
        self.subscription_service = subscription_service
        self.referral_service = referral_service

        self.base_url = (settings.ROLLYPAY_BASE_URL or "https://rollypay.io").rstrip("/")
        self.api_key = (settings.ROLLYPAY_API_KEY or "").strip()
        self.signing_secret = (settings.ROLLYPAY_SIGNING_SECRET or "").strip()
        self.payment_method = (settings.ROLLYPAY_PAYMENT_METHOD or "").strip() or None
        self.return_url = settings.ROLLYPAY_RETURN_URL or f"https://t.me/{default_return_url}"
        self.success_url = settings.ROLLYPAY_SUCCESS_URL or self.return_url
        self.fail_url = settings.ROLLYPAY_FAIL_URL or self.return_url

        self._timeout = ClientTimeout(total=20)
        self._session: Optional[ClientSession] = None

        self.configured: bool = bool(
            settings.ROLLYPAY_ENABLED and self.api_key and self.signing_secret
        )
        if not self.configured:
            logging.warning(
                "RollyPayService initialized but not fully configured. Payments disabled."
            )

    async def _get_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _format_amount(amount: float) -> str:
        quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{quantized:.2f}"

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
            "X-Nonce": str(uuid.uuid4()),
        }

    def _verify_signature(self, raw_body: bytes, timestamp: str, signature: str) -> bool:
        if not self.signing_secret or not timestamp or not signature:
            return False
        payload = timestamp.encode("utf-8") + b"." + raw_body
        expected = hmac.new(
            self.signing_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def create_payment(
        self,
        *,
        payment_db_id: int,
        user_id: int,
        months: float,
        amount: float,
        currency: Optional[str],
        description: str,
        payment_kind: str = "base_subscription",
        promo_code_service=None,
        session=None,
    ) -> Tuple[bool, Dict[str, Any]]:
        if not self.configured:
            logging.error("RollyPayService is not configured. Cannot create payment.")
            return False, {"message": "service_not_configured"}

        payment_kind = normalize_payment_kind(payment_kind)

        original_amount = None
        discount_amount = None
        promo_code_id = None

        if promo_code_service and session:
            from db.dal import active_discount_dal

            active_discount = await active_discount_dal.get_active_discount(session, user_id)
            promo_model = await promo_code_service.get_user_active_discount(
                session,
                user_id,
                payment_kind=payment_kind,
            )
            if active_discount and promo_model:
                discount_pct, _promo_code, max_discount_amount, combined_discount_scope = promo_model
                promo_code_id = active_discount.promo_code_id
                price_details = promo_code_service.calculate_discounted_offer_details(
                    value=months,
                    payment_kind=payment_kind,
                    discount_percentage=discount_pct,
                    max_discount_amount=max_discount_amount,
                    combined_discount_scope=combined_discount_scope,
                )
                if price_details:
                    original_amount = float(price_details["original_price"])
                    discount_amount = float(price_details["discount_amount"])

                try:
                    await payment_dal.update_payment_discount_info(
                        session,
                        payment_db_id,
                        original_amount,
                        discount_amount,
                        promo_code_id,
                    )
                    await session.commit()
                except Exception as exc:
                    logging.warning(
                        "RollyPay: failed to update discount metadata for payment %s: %s",
                        payment_db_id,
                        exc,
                    )

        http_session = await self._get_session()
        url = f"{self.base_url}/api/v1/payments"
        currency_code = (currency or "RUB").upper()
        amount_str = self._format_amount(amount)

        body: Dict[str, Any] = {
            "amount": amount_str,
            "payment_currency": currency_code,
            "order_id": str(payment_db_id),
            "description": description,
            "customer_id": str(user_id),
            "redirect_url": self.return_url,
            "success_redirect_url": self.success_url,
            "fail_redirect_url": self.fail_url,
            "metadata": {
                "payment_db_id": payment_db_id,
                "user_id": user_id,
                "payment_kind": payment_kind,
                "value": months,
            },
        }
        if self.payment_method:
            body["payment_method"] = self.payment_method

        try:
            async with http_session.post(url, json=body, headers=self._build_headers()) as response:
                response_text = await response.text()
                try:
                    response_data = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError:
                    logging.error(
                        "RollyPay create_payment: invalid JSON response: %s",
                        response_text,
                    )
                    return False, {
                        "status": response.status,
                        "message": "invalid_json",
                        "raw": response_text,
                    }

                if response.status != 200:
                    logging.error(
                        "RollyPay create_payment: API returned error (status=%s, body=%s)",
                        response.status,
                        response_data,
                    )
                    return False, {"status": response.status, "message": response_data}

                return True, response_data
        except Exception as exc:
            logging.error("RollyPay create_payment: request failed: %s", exc, exc_info=True)
            return False, {"message": str(exc)}

    async def webhook_route(self, request: web.Request) -> web.Response:
        if not self.configured:
            return web.json_response({"status": False, "msg": "rollypay_disabled"}, status=503)

        try:
            raw_body = await request.read()
        except Exception as exc:
            logging.error("RollyPay webhook: failed to read body: %s", exc)
            return web.json_response({"status": False, "msg": "bad_request"}, status=400)

        signature = (request.headers.get("X-Signature") or "").strip()
        timestamp = (request.headers.get("X-Timestamp") or "").strip()
        if not self._verify_signature(raw_body, timestamp, signature):
            logging.error("RollyPay webhook: invalid signature.")
            return web.json_response({"status": False, "msg": "invalid_signature"}, status=403)

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception as exc:
            logging.error("RollyPay webhook: failed to parse JSON: %s", exc)
            return web.json_response({"status": False, "msg": "bad_request"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"status": False, "msg": "bad_request"}, status=400)

        event_type = str(payload.get("event_type") or "").lower()
        provider_payment_id = str(payload.get("payment_id") or "").strip()
        order_id_raw = payload.get("order_id")
        status = str(payload.get("status") or "").lower()
        amount_raw = payload.get("amount")
        currency_raw = payload.get("currency")

        payment_db_id: Optional[int] = None
        try:
            if isinstance(order_id_raw, int):
                payment_db_id = order_id_raw
            elif isinstance(order_id_raw, str) and order_id_raw.isdigit():
                payment_db_id = int(order_id_raw)
        except Exception:
            payment_db_id = None

        async with self.async_session_factory() as session:
            payment = None
            if payment_db_id is not None:
                payment = await payment_dal.get_payment_by_db_id(session, payment_db_id)
            if not payment and provider_payment_id:
                payment = await payment_dal.get_payment_by_provider_payment_id(session, provider_payment_id)

            if not payment:
                logging.error(
                    "RollyPay webhook: payment not found (order_id=%s, provider_id=%s)",
                    order_id_raw,
                    provider_payment_id,
                )
                return web.json_response({"status": False, "msg": "payment_not_found"}, status=404)

            payment_months = payment.subscription_duration_months or 1
            sale_mode = normalize_payment_kind(payment.kind or "base_subscription")
            provider_id = provider_payment_id or str(payment.payment_id)

            if event_type == "payment.created" or status in {"created", "processing"}:
                try:
                    await payment_dal.update_provider_payment_and_status(
                        session,
                        payment.payment_id,
                        provider_id,
                        "pending_rollypay",
                    )
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error(
                        "RollyPay webhook: failed to update pending status for %s: %s",
                        provider_id,
                        exc,
                    )
                return web.json_response({"status": True})

            if event_type == "payment.paid" or status == "paid":
                if amount_raw is not None:
                    try:
                        incoming_amount = Decimal(str(amount_raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        expected_amount = Decimal(str(payment.amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        if incoming_amount != expected_amount:
                            logging.error(
                                "RollyPay webhook: amount mismatch for payment %s (expected %s, got %s)",
                                payment.payment_id,
                                expected_amount,
                                incoming_amount,
                            )
                            return web.json_response({"status": False, "msg": "amount_mismatch"}, status=400)
                    except Exception as exc:
                        logging.error(
                            "RollyPay webhook: failed to compare amounts for payment %s: %s",
                            payment.payment_id,
                            exc,
                        )
                        return web.json_response({"status": False, "msg": "amount_validation_error"}, status=400)

                if currency_raw:
                    provider_currency = str(currency_raw).upper()
                    expected_currency = str(payment.currency or "").upper()
                    if expected_currency and provider_currency != expected_currency:
                        logging.error(
                            "RollyPay webhook: currency mismatch for payment %s (expected %s, got %s)",
                            payment.payment_id,
                            expected_currency,
                            provider_currency,
                        )
                        return web.json_response({"status": False, "msg": "currency_mismatch"}, status=400)

                try:
                    marked = await payment_dal.mark_provider_payment_succeeded_once(
                        session,
                        payment.payment_id,
                        provider_id,
                    )
                    if not marked:
                        logging.info(
                            "RollyPay webhook: payment %s already processed atomically",
                            payment.payment_id,
                        )
                        return web.json_response({"status": True})

                    activation = await self.subscription_service.activate_subscription(
                        session,
                        payment.user_id,
                        int(payment_months) if not is_traffic_payment_kind(sale_mode) else 0,
                        float(payment.amount),
                        payment.payment_id,
                        promo_code_id_from_payment=payment.promo_code_id,
                        provider="rollypay",
                        sale_mode=sale_mode,
                        traffic_gb=payment_months if is_traffic_payment_kind(sale_mode) else None,
                        payment_kind=sale_mode,
                    )
                    if not activation or not activation.get("end_date"):
                        raise RuntimeError(
                            f"RollyPay webhook: activation failed for payment {payment.payment_id}"
                        )

                    referral_bonus = None
                    if sale_mode == PAYMENT_KIND_BASE_SUBSCRIPTION:
                        referral_bonus = await self.referral_service.apply_referral_bonuses_for_payment(
                            session,
                            payment.user_id,
                            int(payment_months),
                            current_payment_db_id=payment.payment_id,
                            skip_if_active_before_payment=False,
                        )

                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error(
                        "RollyPay webhook: failed to process payment %s: %s",
                        provider_id,
                        exc,
                        exc_info=True,
                    )
                    return web.json_response({"status": False, "msg": "processing_error"}, status=500)

                db_user = await user_dal.get_user_by_id(session, payment.user_id)
                lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k

                raw_config_link = activation.get("subscription_url") if activation else None
                config_link_display, connect_button_url, link_blocked_by_channel = await prepare_paid_config_links(
                    self.settings,
                    session,
                    payment.user_id,
                    self.i18n,
                    lang,
                    raw_config_link,
                )
                config_link_text = config_link_display or _("config_link_not_available")
                final_end = activation.get("end_date") if activation else None
                applied_days = 0
                applied_promo_days = activation.get("applied_promo_bonus_days", 0) if activation else 0

                if referral_bonus and referral_bonus.get("referee_new_end_date"):
                    final_end = referral_bonus["referee_new_end_date"]
                    applied_days = referral_bonus.get("referee_bonus_applied_days", 0)

                traffic_label = str(int(payment_months)) if float(payment_months).is_integer() else f"{payment_months:g}"

                if is_traffic_payment_kind(sale_mode):
                    text = _(
                        "payment_successful_addon_traffic_full",
                        traffic_gb=traffic_label,
                        end_date=final_end.strftime("%Y-%m-%d") if final_end else "",
                        config_link=config_link_text,
                    )
                elif sale_mode == PAYMENT_KIND_ADDON_SUBSCRIPTION:
                    text = _(
                        "payment_successful_addon_full",
                        end_date=final_end.strftime("%Y-%m-%d") if final_end else "",
                        config_link=config_link_text,
                    )
                elif applied_days:
                    inviter_name_display = _("friend_placeholder")
                    if db_user and db_user.referred_by_id:
                        inviter = await user_dal.get_user_by_id(session, db_user.referred_by_id)
                        if inviter:
                            safe_name = sanitize_display_name(inviter.first_name) if inviter.first_name else None
                            if safe_name:
                                inviter_name_display = safe_name
                            elif inviter.username:
                                inviter_name_display = username_for_display(inviter.username, with_at=False)

                    text = _(
                        "payment_successful_with_referral_bonus_full",
                        months=payment_months,
                        base_end_date=activation["end_date"].strftime("%Y-%m-%d") if activation and activation.get("end_date") else final_end.strftime("%Y-%m-%d") if final_end else "",
                        bonus_days=applied_days,
                        final_end_date=final_end.strftime("%Y-%m-%d") if final_end else "",
                        inviter_name=inviter_name_display,
                        config_link=config_link_text,
                    )
                elif applied_promo_days and final_end:
                    text = _(
                        "payment_successful_with_promo_full",
                        months=payment_months,
                        bonus_days=applied_promo_days,
                        end_date=final_end.strftime("%Y-%m-%d"),
                        config_link=config_link_text,
                    )
                else:
                    text = _(
                        "payment_successful_full",
                        months=payment_months,
                        end_date=final_end.strftime("%Y-%m-%d") if final_end else "",
                        config_link=config_link_text,
                    )

                markup = (
                    get_channel_subscription_keyboard(lang, self.i18n, self.settings.REQUIRED_CHANNEL_LINK)
                    if link_blocked_by_channel
                    else get_connect_and_main_keyboard(
                        lang,
                        self.i18n,
                        self.settings,
                        config_link_display,
                        connect_button_url=connect_button_url,
                        preserve_message=True,
                    )
                )
                try:
                    await self.bot.send_message(
                        payment.user_id,
                        text,
                        reply_markup=markup,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    logging.error(
                        "RollyPay webhook: failed to notify user %s: %s",
                        payment.user_id,
                        exc,
                    )

                try:
                    notification_service = NotificationService(self.bot, self.settings, self.i18n)
                    await notification_service.notify_payment_received(
                        user_id=payment.user_id,
                        amount=float(payment.amount),
                        currency=payment.currency,
                        months=int(payment_months) if not is_traffic_payment_kind(sale_mode) else 0,
                        traffic_gb=payment_months if is_traffic_payment_kind(sale_mode) else None,
                        payment_provider="rollypay",
                        username=db_user.username if db_user else None,
                    )
                except Exception as exc:
                    logging.error("RollyPay webhook: failed to notify admins: %s", exc)

                return web.json_response({"status": True})

            if event_type in {"payment.canceled", "payment.chargeback"} or status in {
                "canceled",
                "cancelled",
                "expired",
                "chargeback",
            }:
                target_status = "chargeback" if event_type == "payment.chargeback" or status == "chargeback" else "canceled"
                try:
                    await payment_dal.update_provider_payment_and_status(
                        session,
                        payment.payment_id,
                        provider_id,
                        target_status,
                    )
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logging.error(
                        "RollyPay webhook: failed to mark payment %s as %s: %s",
                        provider_id,
                        target_status,
                        exc,
                    )
                    return web.json_response({"status": False, "msg": "processing_error"}, status=500)

                db_user = payment.user or await user_dal.get_user_by_id(session, payment.user_id)
                lang = db_user.language_code if db_user and db_user.language_code else self.settings.DEFAULT_LANGUAGE
                _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw) if self.i18n else k
                try:
                    await self.bot.send_message(payment.user_id, _("payment_failed"))
                except Exception as exc:
                    logging.debug(
                        "RollyPay webhook: failed to send cancellation message to user %s: %s",
                        payment.user_id,
                        exc,
                    )
                return web.json_response({"status": True})

            logging.warning(
                "RollyPay webhook: unhandled event '%s' with status '%s' for payment %s",
                event_type,
                status,
                provider_id,
            )
            return web.json_response({"status": True})


async def rollypay_webhook_route(request: web.Request) -> web.Response:
    service: RollyPayService = request.app["rollypay_service"]
    return await service.webhook_route(request)
