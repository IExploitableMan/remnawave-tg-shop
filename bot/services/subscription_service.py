import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from bot.middlewares.i18n import JsonI18n
from bot.utils.config_link import prepare_config_links
from bot.utils.date_utils import add_months
from bot.utils.product_kinds import (
    PAYMENT_KIND_ADDON_SUBSCRIPTION,
    PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
    PAYMENT_KIND_BASE_SUBSCRIPTION,
    SUBSCRIPTION_KIND_ADDON,
    SUBSCRIPTION_KIND_BASE,
    normalize_payment_kind,
)
from config.settings import Settings
from db.dal import (
    addon_traffic_dal,
    payment_dal,
    promo_code_dal,
    subscription_dal,
    user_billing_dal,
    user_dal,
)
from db.models import Subscription, User

from .panel_api_service import PanelApiService


class SubscriptionService:
    def __init__(
        self,
        settings: Settings,
        panel_service: PanelApiService,
        bot: Optional[Bot] = None,
        i18n: Optional[JsonI18n] = None,
    ):
        self.settings = settings
        self.panel_service = panel_service
        self.bot = bot
        self.i18n = i18n
        self._addon_worker_task: Optional[asyncio.Task] = None
        self._async_session_factory: Optional[sessionmaker] = None

    async def close(self) -> None:
        if self._addon_worker_task:
            self._addon_worker_task.cancel()
            try:
                await self._addon_worker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.exception("Failed to stop add-on traffic worker")
            finally:
                self._addon_worker_task = None

    async def setup_addon_traffic_worker(self, async_session_factory: sessionmaker) -> None:
        self._async_session_factory = async_session_factory
        if self._addon_worker_task and not self._addon_worker_task.done():
            return
        self._addon_worker_task = asyncio.create_task(
            self._addon_traffic_worker_loop(),
            name="AddonTrafficWorker",
        )
        logging.info("SubscriptionService: add-on traffic worker started.")

    async def _addon_traffic_worker_loop(self) -> None:
        interval = max(30, int(self.settings.ADDON_TRAFFIC_WORKER_INTERVAL_SECONDS or 300))
        while True:
            try:
                if self._async_session_factory:
                    await self._process_addon_traffic_cycle_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("SubscriptionService: unhandled add-on worker error")
            await asyncio.sleep(interval)

    async def _process_addon_traffic_cycle_once(self) -> None:
        if not self._async_session_factory:
            return
        now_utc = datetime.now(timezone.utc)
        async with self._async_session_factory() as session:
            await addon_traffic_dal.mark_expired_topups(session, now=now_utc)
            addon_subs = await subscription_dal.get_all_active_addon_subscriptions(session, now=now_utc)
            for sub in addon_subs:
                try:
                    await self._process_single_addon_subscription(session, sub, now_utc)
                except Exception:
                    logging.exception(
                        "Failed to process add-on traffic state for user %s subscription %s",
                        sub.user_id,
                        sub.subscription_id,
                    )
            await session.commit()

    async def _process_single_addon_subscription(
        self,
        session: AsyncSession,
        addon_sub: Subscription,
        now_utc: datetime,
    ) -> None:
        if addon_sub.end_date <= now_utc:
            await addon_traffic_dal.expire_topups_for_subscription(session, addon_sub.subscription_id)
            await self._set_addon_profile_runtime_state(
                session,
                addon_sub.user_id,
                addon_sub,
                current_used=0,
                force_disable=True,
                status_from_panel="EXPIRED",
            )
            return

        panel_user_data = await self.panel_service.get_user_by_uuid(addon_sub.panel_user_uuid)
        if not panel_user_data:
            logging.warning(
                "Add-on panel user %s not found for user %s",
                addon_sub.panel_user_uuid,
                addon_sub.user_id,
            )
            return

        if addon_sub.traffic_cycle_ends_at and addon_sub.traffic_cycle_ends_at <= now_utc:
            await self._rollover_addon_cycle(session, addon_sub, now_utc)
            panel_user_data = await self.panel_service.get_user_by_uuid(addon_sub.panel_user_uuid) or panel_user_data

        details = await self._sync_addon_usage_state(session, addon_sub, panel_user_data, now_utc)
        await self._apply_addon_notification_policy(session, addon_sub, details, now_utc)
        await self._apply_addon_dependency_policy(session, addon_sub, details, now_utc)

    async def get_user_language(self, session: AsyncSession, user_id: int) -> str:
        user_record = await user_dal.get_user_by_id(session, user_id)
        return user_record.language_code if user_record and user_record.language_code else self.settings.DEFAULT_LANGUAGE

    async def has_had_any_subscription(
        self,
        session: AsyncSession,
        user_id: int,
        kind: str = SUBSCRIPTION_KIND_BASE,
    ) -> bool:
        return await subscription_dal.has_any_subscription_for_user(session, user_id, kind=kind)

    async def has_active_subscription(
        self,
        session: AsyncSession,
        user_id: int,
        kind: str = SUBSCRIPTION_KIND_BASE,
    ) -> bool:
        try:
            user_record = await user_dal.get_user_by_id(session, user_id)
            if not user_record:
                return False
            panel_uuid = self._panel_uuid_for_kind(user_record, kind)
            if not panel_uuid:
                return False
            active_sub = await subscription_dal.get_active_subscription_by_user_id(
                session,
                user_id,
                panel_uuid,
                kind=kind,
            )
            return bool(active_sub and active_sub.end_date and active_sub.end_date > datetime.now(timezone.utc))
        except Exception:
            return False

    async def _notify_admin_panel_user_creation_failed(self, user_id: int, kind: str = SUBSCRIPTION_KIND_BASE):
        if not self.bot or not self.i18n or not self.settings.ADMIN_IDS:
            return
        admin_lang = self.settings.DEFAULT_LANGUAGE
        _adm = lambda k, **kw: self.i18n.gettext(admin_lang, k, **kw)
        suffix = " (addon)" if kind == SUBSCRIPTION_KIND_ADDON else ""
        msg = _adm("admin_panel_user_creation_failed", user_id=user_id) + suffix
        for admin_id in self.settings.ADMIN_IDS:
            try:
                await self.bot.send_message(admin_id, msg)
            except Exception as e:
                logging.error("Failed to notify admin %s about panel user creation failure: %s", admin_id, e)

    @staticmethod
    def _panel_uuid_attr(kind: str) -> str:
        return "addon_panel_user_uuid" if kind == SUBSCRIPTION_KIND_ADDON else "panel_user_uuid"

    @staticmethod
    def _panel_username_for_kind(user_id: int, kind: str) -> str:
        return f"tg_{user_id}_addon" if kind == SUBSCRIPTION_KIND_ADDON else f"tg_{user_id}"

    def _panel_uuid_for_kind(self, db_user: User, kind: str) -> Optional[str]:
        return getattr(db_user, self._panel_uuid_attr(kind), None)

    def _traffic_limit_for_kind(self, kind: str) -> int:
        if kind == SUBSCRIPTION_KIND_ADDON:
            return 0
        return self.settings.user_traffic_limit_bytes

    def _traffic_strategy_for_kind(self, kind: str) -> str:
        if kind == SUBSCRIPTION_KIND_ADDON:
            return "NO_RESET"
        return self.settings.USER_TRAFFIC_STRATEGY

    def _kind_scopes(self, kind: str) -> tuple[Optional[List[str]], Optional[str]]:
        if kind == SUBSCRIPTION_KIND_ADDON:
            return (
                self.settings.parsed_addon_user_squad_uuids,
                self.settings.parsed_addon_user_external_squad_uuid,
            )
        return (
            self.settings.parsed_user_squad_uuids,
            self.settings.parsed_user_external_squad_uuid,
        )

    @staticmethod
    def _build_user_description(db_user: Optional[User]) -> str:
        return "\n".join(
            [
                (db_user.username or "") if db_user else "",
                (db_user.first_name or "") if db_user else "",
                (db_user.last_name or "") if db_user else "",
            ]
        )

    async def _get_or_create_panel_user_link_details(
        self,
        session: AsyncSession,
        user_id: int,
        db_user: Optional[User] = None,
        kind: str = SUBSCRIPTION_KIND_BASE,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
        if not db_user:
            db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            logging.error("_get_or_create_panel_user_link_details: user %s not found.", user_id)
            return None, None, None, False

        attr_name = self._panel_uuid_attr(kind)
        current_local_panel_uuid = getattr(db_user, attr_name, None)
        panel_username = self._panel_username_for_kind(user_id, kind)
        panel_user_obj_from_api = None
        created_now = False

        if kind == SUBSCRIPTION_KIND_BASE:
            panel_users_by_tg_id_list = await self.panel_service.get_users_by_filter(telegram_id=user_id)
            if panel_users_by_tg_id_list and len(panel_users_by_tg_id_list) == 1:
                panel_user_obj_from_api = panel_users_by_tg_id_list[0]
            elif panel_users_by_tg_id_list and len(panel_users_by_tg_id_list) > 1:
                logging.error("Multiple base panel users found for telegramId %s", user_id)
                return None, None, None, False

        if not panel_user_obj_from_api and current_local_panel_uuid:
            panel_user_obj_from_api = await self.panel_service.get_user_by_uuid(current_local_panel_uuid)

        if not panel_user_obj_from_api:
            fetched_by_username_list = await self.panel_service.get_users_by_filter(username=panel_username)
            if fetched_by_username_list and len(fetched_by_username_list) == 1:
                panel_user_obj_from_api = fetched_by_username_list[0]

        if not panel_user_obj_from_api:
            squad_uuids, external_squad_uuid = self._kind_scopes(kind)
            creation_response = await self.panel_service.create_panel_user(
                username_on_panel=panel_username,
                telegram_id=user_id if kind == SUBSCRIPTION_KIND_BASE else None,
                description=self._build_user_description(db_user),
                specific_squad_uuids=squad_uuids,
                external_squad_uuid=external_squad_uuid,
                default_traffic_limit_bytes=self._traffic_limit_for_kind(kind),
                default_traffic_limit_strategy=self._traffic_strategy_for_kind(kind),
            )
            if creation_response and not creation_response.get("error") and creation_response.get("response"):
                panel_user_obj_from_api = creation_response.get("response")
                created_now = True
            elif creation_response and creation_response.get("errorCode") == "A019":
                fetched_by_username_list = await self.panel_service.get_users_by_filter(username=panel_username)
                if fetched_by_username_list and len(fetched_by_username_list) == 1:
                    panel_user_obj_from_api = fetched_by_username_list[0]
            if not panel_user_obj_from_api:
                await self._notify_admin_panel_user_creation_failed(user_id, kind=kind)
                return None, None, None, False

        actual_panel_uuid = panel_user_obj_from_api.get("uuid")
        if not actual_panel_uuid:
            logging.error("Panel user for user %s kind %s has no uuid: %s", user_id, kind, panel_user_obj_from_api)
            return current_local_panel_uuid, None, None, created_now

        conflicting_user = await user_dal.get_user_by_any_panel_uuid(session, actual_panel_uuid)
        if conflicting_user and conflicting_user.user_id != user_id:
            logging.error(
                "Panel uuid %s for kind %s is already linked to different user %s",
                actual_panel_uuid,
                kind,
                conflicting_user.user_id,
            )
            return None, None, None, False

        if current_local_panel_uuid != actual_panel_uuid:
            await user_dal.update_user(session, user_id, {attr_name: actual_panel_uuid})
            setattr(db_user, attr_name, actual_panel_uuid)
            created_now = True

        if kind == SUBSCRIPTION_KIND_BASE:
            panel_telegram_id_from_api = panel_user_obj_from_api.get("telegramId")
            panel_telegram_id_int = None
            if panel_telegram_id_from_api is not None:
                try:
                    panel_telegram_id_int = int(panel_telegram_id_from_api)
                except (TypeError, ValueError):
                    panel_telegram_id_int = None
            if panel_telegram_id_int != user_id:
                await self.panel_service.update_user_details_on_panel(
                    actual_panel_uuid,
                    {
                        "telegramId": user_id,
                        "description": self._build_user_description(db_user),
                    },
                )

        panel_sub_link_id = panel_user_obj_from_api.get("subscriptionUuid") or panel_user_obj_from_api.get("shortUuid")
        panel_short_uuid = panel_user_obj_from_api.get("shortUuid")
        return actual_panel_uuid, panel_sub_link_id, panel_short_uuid, created_now

    async def activate_trial_subscription(self, session: AsyncSession, user_id: int) -> Optional[Dict[str, Any]]:
        if not self.settings.TRIAL_ENABLED or self.settings.TRIAL_DURATION_DAYS <= 0:
            return {"eligible": False, "activated": False, "message_key": "trial_feature_disabled"}

        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            return {"eligible": False, "activated": False, "message_key": "user_not_found_for_trial"}
        if await self.has_had_any_subscription(session, user_id, kind=SUBSCRIPTION_KIND_BASE):
            return {"eligible": False, "activated": False, "message_key": "trial_already_had_subscription_or_trial"}

        panel_user_uuid, panel_sub_link_id, panel_short_uuid, _ = await self._get_or_create_panel_user_link_details(
            session,
            user_id,
            db_user,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        if not panel_user_uuid or not panel_sub_link_id:
            return {"eligible": True, "activated": False, "message_key": "trial_activation_failed_panel_link"}

        start_date = datetime.now(timezone.utc)
        end_date = start_date + timedelta(days=self.settings.TRIAL_DURATION_DAYS)
        await subscription_dal.deactivate_other_active_subscriptions(session, panel_user_uuid, panel_sub_link_id)
        await subscription_dal.upsert_subscription(
            session,
            {
                "user_id": user_id,
                "panel_user_uuid": panel_user_uuid,
                "panel_subscription_uuid": panel_sub_link_id,
                "kind": SUBSCRIPTION_KIND_BASE,
                "start_date": start_date,
                "end_date": end_date,
                "duration_months": 0,
                "is_active": True,
                "status_from_panel": "TRIAL",
                "traffic_limit_bytes": self.settings.trial_traffic_limit_bytes,
                "auto_renew_enabled": False,
            },
        )

        panel_update_payload = self._build_panel_update_payload(
            kind=SUBSCRIPTION_KIND_BASE,
            panel_user_uuid=panel_user_uuid,
            expire_at=end_date,
            status="ACTIVE",
            traffic_limit_bytes=self.settings.trial_traffic_limit_bytes,
            traffic_limit_strategy=self.settings.USER_TRAFFIC_STRATEGY,
            telegram_id=user_id,
        )
        panel_update_payload["description"] = self._build_user_description(db_user)
        updated_panel_user = await self.panel_service.update_user_details_on_panel(panel_user_uuid, panel_update_payload)
        if not updated_panel_user:
            await session.rollback()
            return {"eligible": True, "activated": False, "message_key": "trial_activation_failed_panel_update"}

        await session.commit()
        return {
            "eligible": True,
            "activated": True,
            "end_date": end_date,
            "days": self.settings.TRIAL_DURATION_DAYS,
            "traffic_gb": self.settings.TRIAL_TRAFFIC_LIMIT_GB,
            "panel_user_uuid": panel_user_uuid,
            "panel_short_uuid": updated_panel_user.get("shortUuid", panel_short_uuid),
            "subscription_url": updated_panel_user.get("subscriptionUrl"),
        }

    async def activate_subscription(
        self,
        session: AsyncSession,
        user_id: int,
        months: int,
        payment_amount: float,
        payment_db_id: int,
        promo_code_id_from_payment: Optional[int] = None,
        provider: str = "yookassa",
        sale_mode: str = "subscription",
        traffic_gb: Optional[float] = None,
        payment_kind: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        normalized_payment_kind = (payment_kind or "").strip().lower()
        if not normalized_payment_kind:
            normalized_payment_kind = normalize_payment_kind(sale_mode)

        if normalized_payment_kind == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP:
            target_gb = float(traffic_gb if traffic_gb is not None else months)
            return await self._activate_addon_topup(
                session=session,
                user_id=user_id,
                traffic_gb=target_gb,
                payment_db_id=payment_db_id,
                provider=provider,
            )
        if normalized_payment_kind == PAYMENT_KIND_ADDON_SUBSCRIPTION:
            return await self._activate_addon_subscription(
                session=session,
                user_id=user_id,
                payment_db_id=payment_db_id,
                provider=provider,
                promo_code_id_from_payment=promo_code_id_from_payment,
            )
        return await self._activate_base_subscription(
            session=session,
            user_id=user_id,
            months=months,
            payment_db_id=payment_db_id,
            provider=provider,
            promo_code_id_from_payment=promo_code_id_from_payment,
        )

    async def _activate_base_subscription(
        self,
        session: AsyncSession,
        user_id: int,
        months: int,
        payment_db_id: int,
        provider: str,
        promo_code_id_from_payment: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            logging.error("User %s not found in DB for base activation.", user_id)
            return None

        panel_user_uuid, panel_sub_link_id, panel_short_uuid, _ = await self._get_or_create_panel_user_link_details(
            session,
            user_id,
            db_user,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        if not panel_user_uuid or not panel_sub_link_id:
            return None

        current_active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session,
            user_id,
            panel_user_uuid,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        start_date = datetime.now(timezone.utc)
        if current_active_sub and current_active_sub.end_date and current_active_sub.end_date > start_date:
            start_date = current_active_sub.end_date

        months_int = max(1, int(months or 1))
        end_after_months = add_months(start_date, months_int)
        duration_days_total = (end_after_months - start_date).days
        applied_promo_bonus_days = await self._consume_bonus_promo_for_payment(
            session,
            user_id=user_id,
            payment_db_id=payment_db_id,
            promo_code_id=promo_code_id_from_payment,
            payment_kind=PAYMENT_KIND_BASE_SUBSCRIPTION,
        )
        duration_days_total += applied_promo_bonus_days
        final_end_date = start_date + timedelta(days=duration_days_total)

        await subscription_dal.deactivate_other_active_subscriptions(session, panel_user_uuid, panel_sub_link_id)
        auto_renew_should_enable = False
        if provider == "yookassa" and self.settings.yookassa_autopayments_active:
            auto_renew_should_enable = await user_billing_dal.user_has_saved_payment_method(session, user_id)

        new_or_updated_sub = await subscription_dal.upsert_subscription(
            session,
            {
                "user_id": user_id,
                "panel_user_uuid": panel_user_uuid,
                "panel_subscription_uuid": panel_sub_link_id,
                "kind": SUBSCRIPTION_KIND_BASE,
                "start_date": start_date,
                "end_date": final_end_date,
                "duration_months": months_int,
                "is_active": True,
                "status_from_panel": "ACTIVE",
                "traffic_limit_bytes": self.settings.user_traffic_limit_bytes,
                "provider": provider,
                "skip_notifications": False,
                "auto_renew_enabled": auto_renew_should_enable,
            },
        )

        panel_update_payload = self._build_panel_update_payload(
            kind=SUBSCRIPTION_KIND_BASE,
            panel_user_uuid=panel_user_uuid,
            expire_at=final_end_date,
            status="ACTIVE",
            traffic_limit_bytes=self.settings.user_traffic_limit_bytes,
            telegram_id=user_id,
        )
        panel_update_payload["description"] = self._build_user_description(db_user)
        updated_panel_user = await self.panel_service.update_user_details_on_panel(panel_user_uuid, panel_update_payload)
        if not updated_panel_user:
            return None

        await self._consume_discount_if_present(session, user_id, payment_db_id)
        await self._restore_addon_if_possible(session, user_id)
        return {
            "subscription_id": new_or_updated_sub.subscription_id,
            "end_date": final_end_date,
            "is_active": True,
            "panel_user_uuid": panel_user_uuid,
            "panel_short_uuid": updated_panel_user.get("shortUuid", panel_short_uuid),
            "subscription_url": updated_panel_user.get("subscriptionUrl"),
            "applied_promo_bonus_days": applied_promo_bonus_days,
            "payment_kind": PAYMENT_KIND_BASE_SUBSCRIPTION,
        }

    async def _activate_addon_subscription(
        self,
        session: AsyncSession,
        user_id: int,
        payment_db_id: int,
        provider: str,
        promo_code_id_from_payment: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        if not self.settings.addon_enabled:
            logging.warning("Add-on subscription purchase rejected: add-on is not configured.")
            return None
        base_sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id, kind=SUBSCRIPTION_KIND_BASE)
        if not base_sub:
            logging.warning("Add-on subscription purchase rejected: user %s has no active base subscription.", user_id)
            return None

        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            return None

        panel_user_uuid, panel_sub_link_id, panel_short_uuid, _ = await self._get_or_create_panel_user_link_details(
            session,
            user_id,
            db_user,
            kind=SUBSCRIPTION_KIND_ADDON,
        )
        if not panel_user_uuid or not panel_sub_link_id:
            return None

        now_utc = datetime.now(timezone.utc)
        current_active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session,
            user_id,
            panel_user_uuid,
            kind=SUBSCRIPTION_KIND_ADDON,
        )
        start_date = now_utc
        if current_active_sub and current_active_sub.end_date and current_active_sub.end_date > now_utc:
            start_date = current_active_sub.end_date

        new_end_date = add_months(start_date, 1)
        applied_bonus_days = await self._consume_bonus_promo_for_payment(
            session,
            user_id=user_id,
            payment_db_id=payment_db_id,
            promo_code_id=promo_code_id_from_payment,
            payment_kind=PAYMENT_KIND_ADDON_SUBSCRIPTION,
        )
        if applied_bonus_days:
            new_end_date = new_end_date + timedelta(days=applied_bonus_days)

        traffic_cycle_started_at = None
        traffic_cycle_ends_at = None
        included_remaining = self.settings.addon_monthly_traffic_bytes
        if current_active_sub and current_active_sub.traffic_cycle_ends_at and current_active_sub.traffic_cycle_ends_at > now_utc:
            traffic_cycle_started_at = current_active_sub.traffic_cycle_started_at
            traffic_cycle_ends_at = current_active_sub.traffic_cycle_ends_at
            included_remaining = current_active_sub.included_traffic_remaining_bytes or self.settings.addon_monthly_traffic_bytes
        else:
            traffic_cycle_started_at = now_utc
            traffic_cycle_ends_at = add_months(now_utc, 1)
            if traffic_cycle_ends_at > new_end_date:
                traffic_cycle_ends_at = new_end_date

        new_or_updated_sub = await subscription_dal.upsert_subscription(
            session,
            {
                "user_id": user_id,
                "panel_user_uuid": panel_user_uuid,
                "panel_subscription_uuid": panel_sub_link_id,
                "kind": SUBSCRIPTION_KIND_ADDON,
                "start_date": current_active_sub.start_date if current_active_sub and current_active_sub.start_date else now_utc,
                "end_date": new_end_date,
                "duration_months": 1,
                "is_active": True,
                "status_from_panel": "ACTIVE",
                "traffic_limit_bytes": 0,
                "traffic_used_bytes": current_active_sub.traffic_used_bytes if current_active_sub else 0,
                "provider": provider,
                "skip_notifications": True,
                "auto_renew_enabled": False,
                "included_traffic_bytes": self.settings.addon_monthly_traffic_bytes,
                "included_traffic_remaining_bytes": included_remaining,
                "traffic_cycle_started_at": traffic_cycle_started_at,
                "traffic_cycle_ends_at": traffic_cycle_ends_at,
                "traffic_warning_sent_at": None,
                "traffic_exhausted_sent_at": None,
            },
        )
        await addon_traffic_dal.extend_topups_expiration(session, new_or_updated_sub.subscription_id, new_end_date)
        panel_user_data = await self.panel_service.get_user_by_uuid(panel_user_uuid) or {}
        details = await self._sync_addon_usage_state(session, new_or_updated_sub, panel_user_data, now_utc)
        await self._set_addon_profile_runtime_state(
            session,
            user_id,
            new_or_updated_sub,
            current_used=details["current_used_bytes"],
        )
        await self._consume_discount_if_present(session, user_id, payment_db_id)
        return {
            "subscription_id": new_or_updated_sub.subscription_id,
            "end_date": new_end_date,
            "is_active": True,
            "panel_user_uuid": panel_user_uuid,
            "panel_short_uuid": panel_short_uuid,
            "subscription_url": panel_user_data.get("subscriptionUrl") or await self.panel_service.get_subscription_link(panel_short_uuid or panel_sub_link_id),
            "applied_promo_bonus_days": applied_bonus_days,
            "payment_kind": PAYMENT_KIND_ADDON_SUBSCRIPTION,
            "warning_base_ends_before_addon": base_sub.end_date < new_end_date,
            "base_end_date": base_sub.end_date,
        }

    async def _activate_addon_topup(
        self,
        session: AsyncSession,
        user_id: int,
        traffic_gb: float,
        payment_db_id: int,
        provider: str,
    ) -> Optional[Dict[str, Any]]:
        addon_sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id, kind=SUBSCRIPTION_KIND_ADDON)
        if not addon_sub:
            logging.warning("Add-on top-up rejected: user %s has no active add-on entitlement.", user_id)
            return None

        purchase_bytes = int(float(traffic_gb) * (1024 ** 3))
        await addon_traffic_dal.create_topup(
            session,
            {
                "user_id": user_id,
                "subscription_id": addon_sub.subscription_id,
                "payment_id": payment_db_id,
                "total_bytes": purchase_bytes,
                "remaining_bytes": purchase_bytes,
                "expires_at": addon_sub.end_date,
                "status": "active",
            },
        )
        addon_sub.traffic_warning_sent_at = None
        addon_sub.traffic_exhausted_sent_at = None
        panel_user_data = await self.panel_service.get_user_by_uuid(addon_sub.panel_user_uuid) or {}
        details = await self._sync_addon_usage_state(session, addon_sub, panel_user_data, datetime.now(timezone.utc))
        await self._set_addon_profile_runtime_state(
            session,
            user_id,
            addon_sub,
            current_used=details["current_used_bytes"],
        )
        await self._consume_discount_if_present(session, user_id, payment_db_id)
        return {
            "subscription_id": addon_sub.subscription_id,
            "end_date": addon_sub.end_date,
            "is_active": True,
            "panel_user_uuid": addon_sub.panel_user_uuid,
            "panel_short_uuid": panel_user_data.get("shortUuid"),
            "subscription_url": panel_user_data.get("subscriptionUrl"),
            "applied_promo_bonus_days": 0,
            "payment_kind": PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
            "traffic_limit_bytes": details["panel_limit_bytes"],
            "traffic_purchased_bytes": purchase_bytes,
        }

    async def _consume_bonus_promo_for_payment(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        payment_db_id: int,
        promo_code_id: Optional[int],
        payment_kind: str,
    ) -> int:
        if not promo_code_id:
            return 0
        promo_model = await promo_code_dal.get_promo_code_by_id(session, promo_code_id)
        if not promo_model or promo_model.promo_type != "bonus_days" or not promo_model.is_active:
            return 0
        if payment_kind == PAYMENT_KIND_ADDON_SUBSCRIPTION and not promo_model.applies_to_addon_subscription:
            return 0
        if payment_kind == PAYMENT_KIND_BASE_SUBSCRIPTION and not promo_model.applies_to_base_subscription:
            return 0
        if payment_kind == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP:
            return 0
        if promo_model.current_activations >= promo_model.max_activations:
            return 0

        activation = await promo_code_dal.record_promo_activation(
            session,
            promo_code_id,
            user_id,
            payment_id=payment_db_id,
        )
        if activation:
            await promo_code_dal.increment_promo_code_usage(session, promo_code_id, allow_overflow=True)
        return int(promo_model.bonus_days or 0)

    async def _consume_discount_if_present(self, session: AsyncSession, user_id: int, payment_db_id: int) -> None:
        try:
            promo_code_service = getattr(self, "promo_code_service", None)
            if not promo_code_service:
                from .promo_code_service import PromoCodeService
                promo_code_service = PromoCodeService(self.settings, self, self.bot, self.i18n)
            await promo_code_service.consume_discount(session, user_id, payment_db_id)
        except Exception as e:
            logging.error("Failed to consume discount for user %s payment %s: %s", user_id, payment_db_id, e)

    async def extend_active_subscription_days(
        self,
        session: AsyncSession,
        user_id: int,
        bonus_days: int,
        reason: str = "bonus",
    ) -> Optional[datetime]:
        reason_lower = (reason or "").lower()
        apply_main_traffic_limit = any(keyword in reason_lower for keyword in ("admin", "promo code", "referral", "bonus"))
        user = await user_dal.get_user_by_id(session, user_id)
        if not user:
            return None

        panel_uuid, panel_sub_uuid, _, _ = await self._get_or_create_panel_user_link_details(
            session,
            user_id,
            user,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        if not panel_uuid or not panel_sub_uuid:
            return None

        active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session,
            user_id,
            panel_uuid,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        if not active_sub or not active_sub.end_date:
            start_date = datetime.now(timezone.utc)
            new_end_date_obj = start_date + timedelta(days=bonus_days)
            traffic_limit = self.settings.user_traffic_limit_bytes if apply_main_traffic_limit else self.settings.trial_traffic_limit_bytes
            updated_sub_model = await subscription_dal.upsert_subscription(
                session,
                {
                    "user_id": user_id,
                    "panel_user_uuid": panel_uuid,
                    "panel_subscription_uuid": panel_sub_uuid,
                    "kind": SUBSCRIPTION_KIND_BASE,
                    "start_date": start_date,
                    "end_date": new_end_date_obj,
                    "duration_months": 0,
                    "is_active": True,
                    "status_from_panel": "ACTIVE_BONUS",
                    "traffic_limit_bytes": traffic_limit,
                    "auto_renew_enabled": False,
                },
            )
        else:
            now_utc = datetime.now(timezone.utc)
            start_point = active_sub.end_date if active_sub.end_date > now_utc else now_utc
            new_end_date_obj = start_point + timedelta(days=bonus_days)
            updated_sub_model = await subscription_dal.update_subscription_end_date(session, active_sub.subscription_id, new_end_date_obj)
            if apply_main_traffic_limit and updated_sub_model and updated_sub_model.traffic_limit_bytes != self.settings.user_traffic_limit_bytes:
                updated_sub_model = await subscription_dal.update_subscription(
                    session,
                    updated_sub_model.subscription_id,
                    {"traffic_limit_bytes": self.settings.user_traffic_limit_bytes},
                )

        if not updated_sub_model:
            return None
        panel_update_payload = self._build_panel_update_payload(
            kind=SUBSCRIPTION_KIND_BASE,
            expire_at=new_end_date_obj,
            traffic_limit_bytes=self.settings.user_traffic_limit_bytes if apply_main_traffic_limit else None,
            include_uuid=False,
        )
        await self.panel_service.update_user_details_on_panel(panel_uuid, panel_update_payload)
        await self._restore_addon_if_possible(session, user_id)
        return new_end_date_obj

    async def _fetch_profile_details(
        self,
        session: AsyncSession,
        user_id: int,
        kind: str,
    ) -> Optional[Dict[str, Any]]:
        db_user = await user_dal.get_user_by_id(session, user_id)
        if not db_user:
            return None
        panel_user_uuid = self._panel_uuid_for_kind(db_user, kind)
        if not panel_user_uuid:
            return None

        local_active_sub = await subscription_dal.get_active_subscription_by_user_id(
            session,
            user_id,
            panel_user_uuid,
            kind=kind,
        )
        panel_user_data = await self.panel_service.get_user_by_uuid(panel_user_uuid)
        if not panel_user_data:
            await subscription_dal.deactivate_all_user_subscriptions(session, user_id, kind=kind)
            await user_dal.update_user(session, user_id, {self._panel_uuid_attr(kind): None})
            return None

        panel_end_date = (
            datetime.fromisoformat(panel_user_data["expireAt"].replace("Z", "+00:00"))
            if panel_user_data.get("expireAt")
            else None
        )
        panel_status = panel_user_data.get("status", "UNKNOWN").upper()
        traffic_used = int((panel_user_data.get("userTraffic") or {}).get("usedTrafficBytes") or 0)
        traffic_limit = int(panel_user_data.get("trafficLimitBytes") or 0)
        panel_sub_uuid = panel_user_data.get("subscriptionUuid") or panel_user_data.get("shortUuid")
        raw_config_link = panel_user_data.get("subscriptionUrl")
        display_link, connect_button_url = await prepare_config_links(self.settings, raw_config_link)

        if local_active_sub:
            update_payload_local: Dict[str, Any] = {}
            if panel_end_date and local_active_sub.end_date.replace(microsecond=0) != panel_end_date.replace(microsecond=0):
                update_payload_local["end_date"] = panel_end_date
                update_payload_local["last_notification_sent"] = None
            if local_active_sub.status_from_panel != panel_status:
                update_payload_local["status_from_panel"] = panel_status
            if local_active_sub.traffic_used_bytes != traffic_used:
                update_payload_local["traffic_used_bytes"] = traffic_used
            if local_active_sub.traffic_limit_bytes != traffic_limit:
                update_payload_local["traffic_limit_bytes"] = traffic_limit
            if panel_sub_uuid and local_active_sub.panel_subscription_uuid != panel_sub_uuid:
                update_payload_local["panel_subscription_uuid"] = panel_sub_uuid
            if update_payload_local:
                local_active_sub = await subscription_dal.update_subscription(session, local_active_sub.subscription_id, update_payload_local)

        details: Dict[str, Any] = {
            "user_id": panel_user_data.get("uuid"),
            "subscription_id": local_active_sub.subscription_id if local_active_sub else None,
            "kind": kind,
            "end_date": panel_end_date,
            "status_from_panel": panel_status,
            "config_link": display_link,
            "connect_button_url": connect_button_url,
            "traffic_limit_bytes": traffic_limit,
            "traffic_used_bytes": traffic_used,
            "user_bot_username": db_user.username,
            "is_panel_data": True,
            "max_devices": panel_user_data.get("hwidDeviceLimit") if kind == SUBSCRIPTION_KIND_BASE else None,
        }
        if kind == SUBSCRIPTION_KIND_ADDON and local_active_sub:
            addon_state = await self._sync_addon_usage_state(session, local_active_sub, panel_user_data, datetime.now(timezone.utc))
            base_active = await self.has_active_subscription(session, user_id, kind=SUBSCRIPTION_KIND_BASE)
            details.update(
                {
                    "included_traffic_bytes": local_active_sub.included_traffic_bytes or 0,
                    "included_traffic_remaining_bytes": addon_state["included_remaining_bytes"],
                    "addon_topup_remaining_bytes": addon_state["topup_remaining_bytes"],
                    "traffic_remaining_bytes": addon_state["total_remaining_bytes"],
                    "addon_state": self._derive_addon_state(local_active_sub, base_active, addon_state["total_remaining_bytes"]),
                }
            )
        return details

    async def get_active_subscription_details(self, session: AsyncSession, user_id: int) -> Optional[Dict[str, Any]]:
        return await self._fetch_profile_details(session, user_id, kind=SUBSCRIPTION_KIND_BASE)

    async def get_subscription_overview(self, session: AsyncSession, user_id: int) -> Dict[str, Optional[Dict[str, Any]]]:
        base_details = await self._fetch_profile_details(session, user_id, kind=SUBSCRIPTION_KIND_BASE)
        addon_details = await self._fetch_profile_details(session, user_id, kind=SUBSCRIPTION_KIND_ADDON)
        return {
            "base": base_details,
            "addon": addon_details,
        }

    async def get_subscriptions_ending_soon(self, session: AsyncSession, days_threshold: int) -> List[Dict[str, Any]]:
        subs_models_with_users = await subscription_dal.get_subscriptions_near_expiration(
            session,
            days_threshold,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        results = []
        for sub_model in subs_models_with_users:
            if sub_model.user and sub_model.end_date and not sub_model.skip_notifications:
                days_left = (sub_model.end_date - datetime.now(timezone.utc)).total_seconds() / (24 * 3600)
                results.append(
                    {
                        "user_id": sub_model.user_id,
                        "first_name": sub_model.user.first_name or f"User {sub_model.user_id}",
                        "language_code": sub_model.user.language_code or self.settings.DEFAULT_LANGUAGE,
                        "end_date_str": sub_model.end_date.strftime("%Y-%m-%d"),
                        "days_left": max(0, int(round(days_left))),
                        "subscription_end_date_iso_for_update": sub_model.end_date,
                    }
                )
        return results

    async def charge_subscription_renewal(self, session: AsyncSession, sub: Subscription) -> bool:
        if sub.kind != SUBSCRIPTION_KIND_BASE:
            return True
        if not sub.auto_renew_enabled or not self.settings.yookassa_autopayments_active or sub.provider != "yookassa":
            return True

        from db.dal.user_billing_dal import get_user_default_payment_method

        default_pm = await get_user_default_payment_method(session, sub.user_id)
        if not default_pm:
            return False
        try:
            from .yookassa_service import YooKassaService
            yk: YooKassaService = self.yookassa_service  # type: ignore[attr-defined]
        except Exception:
            yk = None  # type: ignore
        if not yk or not getattr(yk, "configured", False):
            return False

        months = sub.duration_months or 1
        amount = self.settings.subscription_options.get(months)
        if not amount:
            return False

        payment_description = f"Auto-renewal for {months} months"
        payment_record = await payment_dal.create_payment_record(
            session,
            {
                "user_id": sub.user_id,
                "amount": float(amount),
                "currency": "RUB",
                "status": "pending_yookassa",
                "description": payment_description,
                "subscription_duration_months": int(months),
                "provider": "yookassa",
                "kind": PAYMENT_KIND_BASE_SUBSCRIPTION,
            },
        )
        metadata = {
            "user_id": str(sub.user_id),
            "auto_renew_for_subscription_id": str(sub.subscription_id),
            "subscription_months": str(months),
            "payment_db_id": str(payment_record.payment_id),
            "payment_kind": PAYMENT_KIND_BASE_SUBSCRIPTION,
        }
        resp = await yk.create_payment(
            amount=float(amount),
            currency="RUB",
            description=payment_description,
            metadata=metadata,
            payment_method_id=default_pm.provider_payment_method_id,
            save_payment_method=False,
            capture=True,
        )
        if not resp or resp.get("status") not in {"pending", "waiting_for_capture", "succeeded"}:
            return False
        provider_payment_id = resp.get("id")
        if provider_payment_id:
            await payment_dal.update_provider_payment_and_status(
                session,
                payment_db_id=payment_record.payment_id,
                provider_payment_id=provider_payment_id,
                new_status="pending_yookassa",
            )
        return True

    async def update_last_notification_sent(self, session: AsyncSession, user_id: int, subscription_end_date: datetime):
        sub_to_update = await subscription_dal.find_subscription_for_notification_update(
            session,
            user_id,
            subscription_end_date,
            kind=SUBSCRIPTION_KIND_BASE,
        )
        if sub_to_update:
            await subscription_dal.update_subscription_notification_time(
                session,
                sub_to_update.subscription_id,
                datetime.now(timezone.utc),
            )

    def _build_panel_update_payload(
        self,
        *,
        kind: str = SUBSCRIPTION_KIND_BASE,
        panel_user_uuid: Optional[str] = None,
        expire_at: Optional[datetime] = None,
        status: Optional[str] = None,
        traffic_limit_bytes: Optional[int] = None,
        include_uuid: bool = True,
        traffic_limit_strategy: Optional[str] = None,
        telegram_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if include_uuid and panel_user_uuid:
            payload["uuid"] = panel_user_uuid
        if expire_at is not None:
            payload["expireAt"] = expire_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if status is not None:
            payload["status"] = status
        if traffic_limit_bytes is not None:
            payload["trafficLimitBytes"] = int(max(0, traffic_limit_bytes))
            payload["trafficLimitStrategy"] = traffic_limit_strategy or self._traffic_strategy_for_kind(kind)
        squad_uuids, external_squad_uuid = self._kind_scopes(kind)
        if squad_uuids:
            payload["activeInternalSquads"] = squad_uuids
        if external_squad_uuid:
            payload["externalSquadUuid"] = external_squad_uuid
        if kind == SUBSCRIPTION_KIND_BASE and telegram_id is not None:
            payload["telegramId"] = telegram_id
        return payload

    async def _restore_addon_if_possible(self, session: AsyncSession, user_id: int) -> None:
        addon_sub = await subscription_dal.get_active_subscription_by_user_id(session, user_id, kind=SUBSCRIPTION_KIND_ADDON)
        if not addon_sub:
            return
        await self._set_addon_profile_runtime_state(session, user_id, addon_sub, current_used=addon_sub.traffic_used_bytes or 0)

    async def _sync_addon_usage_state(
        self,
        session: AsyncSession,
        addon_sub: Subscription,
        panel_user_data: Dict[str, Any],
        now_utc: datetime,
    ) -> Dict[str, int]:
        current_used = int((panel_user_data.get("userTraffic") or {}).get("usedTrafficBytes") or addon_sub.traffic_used_bytes or 0)
        addon_sub.traffic_used_bytes = current_used
        included_total = int(addon_sub.included_traffic_bytes or self.settings.addon_monthly_traffic_bytes or 0)
        nonexpired_topups = await addon_traffic_dal.get_nonexpired_topups_for_subscription(
            session,
            addon_sub.subscription_id,
            now=now_utc,
        )
        total_topup_bytes = sum(int(item.total_bytes or 0) for item in nonexpired_topups)
        remaining_topup_bytes = sum(int(item.remaining_bytes or 0) for item in nonexpired_topups)
        current_topup_consumed = total_topup_bytes - remaining_topup_bytes
        desired_topup_consumed = max(0, current_used - included_total)
        if desired_topup_consumed > current_topup_consumed:
            await addon_traffic_dal.consume_topup_bytes_fifo(
                session,
                addon_sub.subscription_id,
                desired_topup_consumed - current_topup_consumed,
                now=now_utc,
            )
            remaining_topup_bytes = await addon_traffic_dal.get_total_active_topup_remaining_bytes(
                session,
                addon_sub.subscription_id,
                now=now_utc,
            )
        included_remaining = max(0, included_total - current_used)
        total_remaining = included_remaining + remaining_topup_bytes
        panel_limit = current_used + total_remaining

        addon_sub.included_traffic_bytes = included_total
        addon_sub.included_traffic_remaining_bytes = included_remaining
        addon_sub.traffic_limit_bytes = panel_limit
        await session.flush()
        return {
            "current_used_bytes": current_used,
            "included_remaining_bytes": included_remaining,
            "topup_remaining_bytes": remaining_topup_bytes,
            "total_remaining_bytes": total_remaining,
            "panel_limit_bytes": panel_limit,
        }

    async def _set_addon_profile_runtime_state(
        self,
        session: AsyncSession,
        user_id: int,
        addon_sub: Subscription,
        current_used: int,
        force_disable: bool = False,
        status_from_panel: Optional[str] = None,
    ) -> None:
        now_utc = datetime.now(timezone.utc)
        base_active = await self.has_active_subscription(session, user_id, kind=SUBSCRIPTION_KIND_BASE)
        remaining_topup = await addon_traffic_dal.get_total_active_topup_remaining_bytes(
            session,
            addon_sub.subscription_id,
            now=now_utc,
        )
        included_remaining = int(addon_sub.included_traffic_remaining_bytes or 0)
        total_remaining = included_remaining + remaining_topup
        should_disable = force_disable or not base_active or total_remaining <= 0 or addon_sub.end_date <= now_utc
        panel_status = "DISABLED" if should_disable else "ACTIVE"
        local_status = status_from_panel
        if not local_status:
            if addon_sub.end_date <= now_utc:
                local_status = "EXPIRED"
            elif not base_active:
                local_status = "SUSPENDED_BASE_REQUIRED"
            elif total_remaining <= 0:
                local_status = "TRAFFIC_EXHAUSTED"
            else:
                local_status = "ACTIVE"

        panel_update_payload = self._build_panel_update_payload(
            kind=SUBSCRIPTION_KIND_ADDON,
            panel_user_uuid=addon_sub.panel_user_uuid,
            expire_at=addon_sub.end_date,
            status=panel_status,
            traffic_limit_bytes=max(0, current_used + total_remaining),
            traffic_limit_strategy="NO_RESET",
        )
        await self.panel_service.update_user_details_on_panel(addon_sub.panel_user_uuid, panel_update_payload)
        await subscription_dal.update_subscription(
            session,
            addon_sub.subscription_id,
            {
                "status_from_panel": local_status,
                "traffic_limit_bytes": max(0, current_used + total_remaining),
                "traffic_used_bytes": current_used,
                "traffic_warning_sent_at": None if total_remaining > 0 else addon_sub.traffic_warning_sent_at,
            },
        )

    async def _rollover_addon_cycle(
        self,
        session: AsyncSession,
        addon_sub: Subscription,
        now_utc: datetime,
    ) -> None:
        await self.panel_service.reset_user_traffic(
            addon_sub.panel_user_uuid,
            log_response=False,
        )
        cycle_start = addon_sub.traffic_cycle_ends_at or now_utc
        cycle_end = add_months(cycle_start, 1)
        if cycle_end > addon_sub.end_date:
            cycle_end = addon_sub.end_date
        await subscription_dal.update_subscription(
            session,
            addon_sub.subscription_id,
            {
                "traffic_used_bytes": 0,
                "included_traffic_bytes": self.settings.addon_monthly_traffic_bytes,
                "included_traffic_remaining_bytes": self.settings.addon_monthly_traffic_bytes,
                "traffic_cycle_started_at": cycle_start,
                "traffic_cycle_ends_at": cycle_end,
                "traffic_warning_sent_at": None,
                "traffic_exhausted_sent_at": None,
            },
        )

    async def _apply_addon_notification_policy(
        self,
        session: AsyncSession,
        addon_sub: Subscription,
        details: Dict[str, int],
        now_utc: datetime,
    ) -> None:
        if not self.bot or not self.i18n:
            return
        user = await user_dal.get_user_by_id(session, addon_sub.user_id)
        if not user:
            return
        lang = user.language_code or self.settings.DEFAULT_LANGUAGE
        _ = lambda k, **kw: self.i18n.gettext(lang, k, **kw)
        total_limit = int((addon_sub.included_traffic_bytes or 0) + details["topup_remaining_bytes"] + details["current_used_bytes"])
        should_warn = self._should_warn_on_addon_traffic(total_limit, details["total_remaining_bytes"])
        if details["total_remaining_bytes"] <= 0 and addon_sub.traffic_exhausted_sent_at is None:
            try:
                await self.bot.send_message(addon_sub.user_id, _("addon_traffic_exhausted_notification"))
                addon_sub.traffic_exhausted_sent_at = now_utc
            except Exception:
                logging.exception("Failed to send add-on exhausted notification to user %s", addon_sub.user_id)
        elif should_warn and addon_sub.traffic_warning_sent_at is None:
            try:
                remaining_gb = details["total_remaining_bytes"] / (1024 ** 3)
                await self.bot.send_message(
                    addon_sub.user_id,
                    _("addon_traffic_warning_notification", traffic_left=f"{remaining_gb:.2f}"),
                )
                addon_sub.traffic_warning_sent_at = now_utc
            except Exception:
                logging.exception("Failed to send add-on traffic warning notification to user %s", addon_sub.user_id)

    async def _apply_addon_dependency_policy(
        self,
        session: AsyncSession,
        addon_sub: Subscription,
        details: Dict[str, int],
        now_utc: datetime,
    ) -> None:
        await self._set_addon_profile_runtime_state(
            session,
            addon_sub.user_id,
            addon_sub,
            current_used=details["current_used_bytes"],
        )

    def _should_warn_on_addon_traffic(self, total_limit_bytes: int, remaining_bytes: int) -> bool:
        percent_threshold = int(self.settings.ADDON_TRAFFIC_WARNING_PERCENT or 0)
        gb_threshold = float(self.settings.ADDON_TRAFFIC_WARNING_GB or 0.0)
        gb_threshold_bytes = int(gb_threshold * (1024 ** 3)) if gb_threshold > 0 else 0
        if percent_threshold <= 0 and gb_threshold_bytes <= 0:
            return False
        if remaining_bytes <= 0:
            return False
        if gb_threshold_bytes > 0 and remaining_bytes < gb_threshold_bytes:
            return True
        if percent_threshold > 0 and total_limit_bytes > 0:
            used_percent = ((total_limit_bytes - remaining_bytes) / total_limit_bytes) * 100
            return used_percent >= percent_threshold
        return gb_threshold_bytes > 0 and remaining_bytes <= gb_threshold_bytes

    @staticmethod
    def _derive_addon_state(addon_sub: Subscription, base_active: bool, total_remaining_bytes: int) -> str:
        now_utc = datetime.now(timezone.utc)
        if addon_sub.end_date <= now_utc:
            return "expired"
        if not base_active:
            return "suspended"
        if total_remaining_bytes <= 0:
            return "limited"
        return "active"
