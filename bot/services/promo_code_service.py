import logging
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Tuple
from aiogram import Bot
from sqlalchemy.orm import sessionmaker

from config.settings import Settings

from db.dal import promo_code_dal, user_dal, active_discount_dal, payment_dal

from .subscription_service import SubscriptionService
from bot.middlewares.i18n import JsonI18n
from .notification_service import NotificationService
from bot.utils.product_kinds import (
    PAYMENT_KIND_ADDON_SUBSCRIPTION,
    PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
    PAYMENT_KIND_BASE_SUBSCRIPTION,
    PAYMENT_KIND_COMBINED_SUBSCRIPTION,
    normalize_payment_kind,
)
from bot.utils.product_offers import resolve_base_price


class PromoCodeService:

    def __init__(self, settings: Settings,
                 subscription_service: SubscriptionService, bot: Bot,
                 i18n: JsonI18n):
        self.settings = settings
        self.subscription_service = subscription_service
        self.bot = bot
        self.i18n = i18n
        self.discount_payment_timeout_minutes = max(
            1,
            int(getattr(settings, "DISCOUNT_PROMO_PAYMENT_TIMEOUT_MINUTES", 10) or 10),
        )
        self._discount_expiration_task: Optional[asyncio.Task] = None
        self._async_session_factory: Optional[sessionmaker] = None

    async def setup_discount_expiration_worker(
        self,
        async_session_factory: sessionmaker,
    ) -> None:
        """Attach DB session factory and start background cleanup loop."""
        self._async_session_factory = async_session_factory
        if self._discount_expiration_task and not self._discount_expiration_task.done():
            return
        self._discount_expiration_task = asyncio.create_task(
            self._discount_expiration_loop(),
            name="PromoDiscountExpirationLoop",
        )
        logging.info("PromoCodeService: started discount expiration background worker.")

    async def close(self) -> None:
        """Gracefully stop background workers."""
        if not self._discount_expiration_task:
            return
        self._discount_expiration_task.cancel()
        try:
            await self._discount_expiration_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.exception("PromoCodeService: failed while stopping expiration worker")
        finally:
            self._discount_expiration_task = None

    async def _validate_promo_constraints(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        promo_data,
        user_lang: str,
        payment_kind: str = "base_subscription",
    ) -> Optional[str]:
        _ = lambda k, **kw: self.i18n.gettext(user_lang, k, **kw)
        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            return _("error_occurred_try_again")

        min_registration_date = getattr(promo_data, "min_user_registration_date", None)
        registration_direction = getattr(promo_data, "registration_date_direction", "after") or "after"
        if min_registration_date and db_user.registration_date:
            if registration_direction == "before" and db_user.registration_date > min_registration_date:
                return _(
                    "promo_code_user_registered_too_late",
                    code=promo_data.code,
                    required_date=min_registration_date.strftime("%Y-%m-%d"),
                )
            if registration_direction != "before" and db_user.registration_date < min_registration_date:
                return _(
                    "promo_code_user_registered_too_early",
                    code=promo_data.code,
                    required_date=min_registration_date.strftime("%Y-%m-%d"),
                )

        requires_addon = normalize_payment_kind(payment_kind) in {
                PAYMENT_KIND_ADDON_SUBSCRIPTION,
                PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
        }
        required_kind = "addon" if requires_addon else "base"
        has_active_required_subscription = await self.subscription_service.has_active_subscription(
            session,
            user_id,
            kind=required_kind,
        )

        subscription_presence_mode = getattr(
            promo_data,
            "subscription_presence_mode",
            "active_only" if getattr(promo_data, "renewal_only", False) else "any",
        ) or "any"
        if subscription_presence_mode == "active_only":
            if not has_active_required_subscription:
                return _("promo_code_only_for_renewal", code=promo_data.code)
        elif subscription_presence_mode == "inactive_only" and has_active_required_subscription:
            return _("promo_code_only_without_active_subscription", code=promo_data.code)

        return None

    @staticmethod
    def _promo_applies_to_payment_kind(promo, payment_kind: str) -> bool:
        normalized = normalize_payment_kind(payment_kind)
        if normalized == PAYMENT_KIND_COMBINED_SUBSCRIPTION:
            return bool(getattr(promo, "applies_to_combined_subscription", False))
        if normalized == PAYMENT_KIND_ADDON_SUBSCRIPTION:
            return bool(getattr(promo, "applies_to_addon_subscription", False))
        if normalized == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP:
            return bool(getattr(promo, "applies_to_addon_traffic_topup", False))
        return bool(getattr(promo, "applies_to_base_subscription", False))

    def calculate_discounted_offer_details(
        self,
        *,
        value: float,
        payment_kind: str,
        discount_percentage: int,
        max_discount_amount: Optional[float] = None,
        combined_discount_scope: str = "base_only",
        stars: bool = False,
    ) -> Optional[dict[str, float | bool | str]]:
        normalized_payment_kind = normalize_payment_kind(payment_kind)
        original_price = resolve_base_price(
            self.settings,
            value,
            normalized_payment_kind,
            stars=stars,
        )
        if original_price is None:
            return None

        original_price_float = float(original_price)
        discountable_price = original_price_float
        discount_scope_applied = "full"
        if normalized_payment_kind == PAYMENT_KIND_COMBINED_SUBSCRIPTION and combined_discount_scope == "base_only":
            base_component_price = resolve_base_price(
                self.settings,
                value,
                PAYMENT_KIND_BASE_SUBSCRIPTION,
                stars=stars,
            )
            if base_component_price is not None:
                discountable_price = min(float(base_component_price), original_price_float)
                discount_scope_applied = "base_only"

        raw_discount_amount = round(discountable_price * (discount_percentage / 100), 2)
        discount_amount = raw_discount_amount
        cap_applied = False
        if max_discount_amount is not None:
            capped_discount_amount = round(float(max_discount_amount), 2)
            if discount_amount > capped_discount_amount:
                discount_amount = capped_discount_amount
                cap_applied = True

        final_price = round(original_price_float - discount_amount, 2)
        if final_price < 0:
            discount_amount = original_price_float
            final_price = 0.0

        return {
            "original_price": original_price_float,
            "final_price": final_price,
            "discount_amount": round(discount_amount, 2),
            "cap_applied": cap_applied,
            "discount_scope_applied": discount_scope_applied,
        }

    async def _discount_expiration_loop(self) -> None:
        """Periodically clears expired discount reservations and notifies users."""
        while True:
            try:
                if not self._async_session_factory:
                    await asyncio.sleep(30)
                    continue

                await self._process_expired_discounts_once()
            except asyncio.CancelledError:
                logging.info("PromoCodeService: discount expiration loop cancelled.")
                raise
            except Exception:
                logging.exception("PromoCodeService: unhandled error in discount expiration loop")

            await asyncio.sleep(30)

    async def _process_expired_discounts_once(self) -> None:
        if not self._async_session_factory:
            return

        now_utc = datetime.now(timezone.utc)
        notifications_to_send: list[tuple[int, str]] = []

        async with self._async_session_factory() as session:
            expired_discounts = await active_discount_dal.get_expired_active_discounts(
                session,
                now=now_utc,
                limit=100,
            )
            if not expired_discounts:
                return

            for expired in expired_discounts:
                cleared = await active_discount_dal.clear_active_discount_if_matches(
                    session,
                    user_id=expired.user_id,
                    promo_code_id=expired.promo_code_id,
                    expires_at_lte=now_utc,
                )
                if not cleared:
                    continue

                await promo_code_dal.decrement_promo_code_usage(session, expired.promo_code_id)

                db_user = await user_dal.get_user_by_id(session, expired.user_id)
                user_lang = (
                    db_user.language_code
                    if db_user and db_user.language_code
                    else self.settings.DEFAULT_LANGUAGE
                )
                promo = await promo_code_dal.get_promo_code_by_id(session, expired.promo_code_id)
                promo_code = promo.code if promo else ""
                message_text = self.i18n.gettext(
                    user_lang,
                    "discount_promo_expired_need_reactivate",
                    code_part=(f" (<code>{promo_code}</code>)" if promo_code else ""),
                )

                notifications_to_send.append((expired.user_id, message_text))

                logging.info(
                    "Expired discount reservation removed: user=%s, promo=%s",
                    expired.user_id,
                    expired.promo_code_id,
                )

            await session.commit()

        for user_id, message_text in notifications_to_send:
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=message_text,
                    parse_mode="HTML",
                )
            except Exception:
                logging.exception(
                    "Failed to send discount expiration message to user %s",
                    user_id,
                )

    async def apply_promo_code(
        self,
        session: AsyncSession,
        user_id: int,
        code_input: str,
        user_lang: str,
    ) -> Tuple[bool, datetime | str]:
        _ = lambda k, **kw: self.i18n.gettext(user_lang, k, **kw)
        code_input_upper = code_input.strip().upper()

        promo_data = await promo_code_dal.get_active_bonus_promo_code_by_code_str(
            session,
            code_input_upper,
        )

        if not promo_data:
            return False, _("promo_code_not_found", code=code_input_upper)

        constraint_error = await self._validate_promo_constraints(
            session,
            user_id=user_id,
            promo_data=promo_data,
            user_lang=user_lang,
            payment_kind="base_subscription",
        )
        if constraint_error:
            return False, constraint_error

        existing_activation = await promo_code_dal.get_user_activation_for_promo(
            session, promo_data.promo_code_id, user_id)
        if existing_activation:
            return False, _("promo_code_already_used_by_user",
                            code=code_input_upper)

        bonus_days = promo_data.bonus_days

        new_end_date = await self.subscription_service.extend_active_subscription_days(
            session=session,
            user_id=user_id,
            bonus_days=bonus_days,
            reason=f"promo code {code_input_upper}")

        if new_end_date:
            activation_recorded = await promo_code_dal.record_promo_activation(
                session, promo_data.promo_code_id, user_id, payment_id=None)
            promo_incremented = await promo_code_dal.increment_promo_code_usage(
                session, promo_data.promo_code_id)

            if activation_recorded and promo_incremented:
                # Send notification about promo activation
                try:
                    notification_service = NotificationService(self.bot, self.settings, self.i18n)
                    user = await user_dal.get_user_by_id(session, user_id)
                    await notification_service.notify_promo_activation(
                        user_id=user_id,
                        promo_code=code_input_upper,
                        bonus_days=bonus_days,
                        username=user.username if user else None
                    )
                except Exception as e:
                    logging.error(f"Failed to send promo activation notification: {e}")
                
                return True, new_end_date
            else:

                logging.error(
                    f"Failed to record activation or increment usage for promo {promo_data.code} by user {user_id}"
                )
                return False, _("error_applying_promo_bonus")
        else:
            return False, _("error_applying_promo_bonus")

    async def apply_traffic_voucher_code(
        self,
        session: AsyncSession,
        user_id: int,
        code_input: str,
        user_lang: str,
    ) -> Tuple[bool, dict[str, object] | str]:
        _ = lambda k, **kw: self.i18n.gettext(user_lang, k, **kw)
        code_input_upper = code_input.strip().upper()

        promo_data = await promo_code_dal.get_promo_code_by_code(session, code_input_upper)
        if not promo_data or promo_data.promo_type != "traffic_gb" or not promo_data.is_active:
            return False, _("promo_code_not_found", code=code_input_upper)

        if promo_data.current_activations >= promo_data.max_activations:
            return False, _("promo_code_not_found", code=code_input_upper)

        if promo_data.valid_until and promo_data.valid_until <= datetime.now(timezone.utc):
            return False, _("promo_code_not_found", code=code_input_upper)

        constraint_error = await self._validate_promo_constraints(
            session,
            user_id=user_id,
            promo_data=promo_data,
            user_lang=user_lang,
            payment_kind=PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
        )
        if constraint_error:
            return False, constraint_error

        existing_activation = await promo_code_dal.get_user_activation_for_promo(
            session,
            promo_data.promo_code_id,
            user_id,
        )
        if existing_activation:
            return False, _("promo_code_already_used_by_user", code=code_input_upper)

        traffic_gb = float(promo_data.traffic_amount_gb or 0)
        if traffic_gb <= 0:
            return False, _("error_occurred_try_again")

        activation = await self.subscription_service.grant_addon_topup_via_voucher(
            session=session,
            user_id=user_id,
            traffic_gb=traffic_gb,
            promo_code=code_input_upper,
        )
        if not activation:
            return False, _("traffic_voucher_requires_active_addon", code=code_input_upper)

        activation_recorded = await promo_code_dal.record_promo_activation(
            session,
            promo_data.promo_code_id,
            user_id,
            payment_id=None,
        )
        promo_incremented = await promo_code_dal.increment_promo_code_usage(
            session,
            promo_data.promo_code_id,
        )
        if not activation_recorded or not promo_incremented:
            return False, _("error_occurred_try_again")

        try:
            notification_service = NotificationService(self.bot, self.settings, self.i18n)
            user = await user_dal.get_user_by_id(session, user_id)
            await notification_service.notify_traffic_voucher_activation(
                user_id=user_id,
                promo_code=code_input_upper,
                traffic_gb=traffic_gb,
                username=user.username if user else None,
            )
        except Exception as e:
            logging.error("Failed to send traffic voucher activation notification: %s", e)

        activation["traffic_gb"] = traffic_gb
        return True, activation

    async def apply_discount_promo_code(
        self,
        session: AsyncSession,
        user_id: int,
        code_input: str,
        user_lang: str,
        payment_kind: str = "base_subscription",
    ) -> Tuple[bool, int | str]:
        """
        Apply a discount promo code (sets active discount for user).
        Returns: (success: bool, discount_percentage or error_message)
        """
        _ = lambda k, **kw: self.i18n.gettext(user_lang, k, **kw)
        code_input_upper = code_input.strip().upper()

        # Check if user already has an active discount
        existing_discount = await active_discount_dal.get_active_discount(
            session,
            user_id,
            include_expired=True,
        )
        if existing_discount:
            now_utc = datetime.now(timezone.utc)
            if existing_discount.expires_at <= now_utc:
                cleared = await active_discount_dal.clear_active_discount_if_expired(
                    session,
                    user_id,
                    now=now_utc,
                )
                if cleared:
                    await promo_code_dal.decrement_promo_code_usage(
                        session,
                        existing_discount.promo_code_id,
                    )
                existing_discount = None

        if existing_discount:
            # Get the promo code for the existing discount
            existing_promo = await promo_code_dal.get_promo_code_by_id(
                session, existing_discount.promo_code_id
            )
            if existing_promo:
                return False, _("discount_promo_already_active",
                               code=existing_promo.code,
                               discount_pct=existing_discount.discount_percentage)
            else:
                # Existing discount but promo not found - clear it and continue
                await active_discount_dal.clear_active_discount(session, user_id)

        # Get discount promo code
        promo_data = await promo_code_dal.get_active_discount_promo_code_by_code_str(
            session, code_input_upper, payment_kind=payment_kind
        )

        if not promo_data:
            return False, _("promo_code_not_found_or_not_discount", code=code_input_upper)

        constraint_error = await self._validate_promo_constraints(
            session,
            user_id=user_id,
            promo_data=promo_data,
            user_lang=user_lang,
            payment_kind=payment_kind,
        )
        if constraint_error:
            return False, constraint_error

        # Check if user already used this code
        existing_activation = await promo_code_dal.get_user_activation_for_promo(
            session, promo_data.promo_code_id, user_id
        )
        if existing_activation:
            return False, _("promo_code_already_used_by_user", code=code_input_upper)

        # Reserve discount for limited time and count activation immediately
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=self.discount_payment_timeout_minutes,
        )
        active_discount = await active_discount_dal.set_active_discount(
            session,
            user_id=user_id,
            promo_code_id=promo_data.promo_code_id,
            discount_percentage=promo_data.discount_percentage,
            expires_at=expires_at,
            payment_kind=payment_kind,
        )

        if not active_discount:
            # This shouldn't happen since we checked above, but just in case
            return False, _("error_applying_promo_discount")

        promo_incremented = await promo_code_dal.increment_promo_code_usage(
            session,
            promo_data.promo_code_id,
        )
        if not promo_incremented:
            await active_discount_dal.clear_active_discount_if_matches(
                session,
                user_id=user_id,
                promo_code_id=promo_data.promo_code_id,
                payment_kind=payment_kind,
            )
            return False, _("promo_code_not_found_or_not_discount", code=code_input_upper)

        logging.info(
            f"Discount promo code {code_input_upper} activated for user {user_id}: "
            f"{promo_data.discount_percentage}% off until {expires_at.isoformat()}"
        )
        return True, promo_data.discount_percentage

    async def get_user_active_discount(
        self,
        session: AsyncSession,
        user_id: int,
        payment_kind: str = "base_subscription",
    ) -> Optional[Tuple[int, str, Optional[float], str]]:
        """
        Get user's active discount if any.
        Returns: (discount_percentage, promo_code, max_discount_amount, combined_discount_scope) or None
        """
        active_discount = await active_discount_dal.get_active_discount(
            session,
            user_id,
            include_expired=True,
        )
        if not active_discount:
            return None

        now_utc = datetime.now(timezone.utc)
        if active_discount.expires_at <= now_utc:
            cleared = await active_discount_dal.clear_active_discount_if_expired(
                session,
                user_id,
                now=now_utc,
            )
            if cleared:
                await promo_code_dal.decrement_promo_code_usage(
                    session,
                    active_discount.promo_code_id,
                )
            return None

        # Fetch promo code for code string
        promo = await promo_code_dal.get_promo_code_by_id(
            session, active_discount.promo_code_id
        )
        if not promo:
            # Discount exists but promo not found - clear it
            await active_discount_dal.clear_active_discount(session, user_id)
            return None

        # Check if promo code has expired
        if promo.valid_until and promo.valid_until <= datetime.now(timezone.utc):
            # Promo code expired - clear the discount
            logging.info(
                f"Promo code {promo.code} expired (valid_until: {promo.valid_until}). "
                f"Clearing active discount for user {user_id}"
            )
            cleared = await active_discount_dal.clear_active_discount(session, user_id)
            if cleared:
                await promo_code_dal.decrement_promo_code_usage(session, promo.promo_code_id)
            return None

        if not self._promo_applies_to_payment_kind(promo, payment_kind):
            return None

        return (
            active_discount.discount_percentage,
            promo.code,
            getattr(promo, "max_discount_amount", None),
            getattr(promo, "combined_discount_scope", "base_only") or "base_only",
        )

    def calculate_discounted_price(
        self,
        original_price: float,
        discount_percentage: int,
        max_discount_amount: Optional[float] = None,
    ) -> Tuple[float, float]:
        """
        Calculate discounted price and discount amount.
        Returns: (final_price, discount_amount)
        """
        discount_amount = round(original_price * (discount_percentage / 100), 2)
        if max_discount_amount is not None:
            discount_amount = min(discount_amount, round(float(max_discount_amount), 2))
        final_price = round(original_price - discount_amount, 2)

        # Ensure price doesn't go negative
        if final_price < 0:
            final_price = 0
            discount_amount = original_price

        return final_price, discount_amount

    async def consume_discount(
        self,
        session: AsyncSession,
        user_id: int,
        payment_id: int,
        payment_kind: Optional[str] = None,
    ) -> bool:
        """
        Consume discount after successful payment.

        The payment record is the source of truth. Even if the active reservation was
        concurrently expired/cleared, we still record promo activation and reconcile
        current_activations so successful discounted payments are always accounted for.
        """
        payment_record = await payment_dal.get_payment_by_db_id(session, payment_id)
        if not payment_record:
            logging.warning(
                "Payment %s not found for discount consumption (user %s).",
                payment_id,
                user_id,
            )
            return False

        if not payment_record.discount_applied:
            return False

        promo_code_id = payment_record.promo_code_id
        if not promo_code_id:
            logging.warning(
                "Payment %s for user %s has discount_applied but no promo_code_id.",
                payment_id,
                user_id,
            )
            return False

        existing_activation = await promo_code_dal.get_user_activation_for_promo(
            session, promo_code_id, user_id
        )

        activation_created = False
        if existing_activation:
            if existing_activation.payment_id is None:
                updated_payment = await promo_code_dal.set_activation_payment_id(
                    session, promo_code_id, user_id, payment_id
                )
                if updated_payment:
                    logging.info(
                        "Linked discount promo %s activation to payment %s for user %s.",
                        promo_code_id,
                        payment_id,
                        user_id,
                    )
        else:
            activation_recorded = await promo_code_dal.record_promo_activation(
                session,
                promo_code_id,
                user_id,
                payment_id=payment_id,
            )
            if not activation_recorded:
                logging.error(
                    "Failed to record discount activation for user %s, promo %s.",
                    user_id,
                    promo_code_id,
                )
                return False
            activation_created = True

        active_discount = await active_discount_dal.get_active_discount(
            session,
            user_id,
            include_expired=True,
        )

        # Reservation is best-effort cleanup at this point; payment success already happened.
        if active_discount and active_discount.promo_code_id == promo_code_id:
            await active_discount_dal.clear_active_discount_if_matches(
                session,
                user_id=user_id,
                promo_code_id=promo_code_id,
            )
        elif active_discount and active_discount.promo_code_id != promo_code_id:
            logging.info(
                "Active discount promo %s differs from payment promo %s during consumption.",
                active_discount.promo_code_id,
                promo_code_id,
            )
        else:
            logging.info(
                "Discount reservation already absent at consumption time (user=%s, promo=%s, payment=%s)",
                user_id,
                promo_code_id,
                payment_id,
            )

        # If reservation was already expired/removed and we had to create activation now,
        # restore current_activations to match the successful payment.
        if activation_created:
            await promo_code_dal.increment_promo_code_usage(
                session,
                promo_code_id,
                allow_overflow=True,
            )

        await session.flush()
        logging.info(
            "Discount consumed for user %s, promo %s, payment %s",
            user_id,
            promo_code_id,
            payment_id,
        )
        return True
