import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

try:
    from bot.services import promo_code_service as promo_service_module
    from bot.utils.product_kinds import (
        PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
        PAYMENT_KIND_BASE_SUBSCRIPTION,
        PAYMENT_KIND_COMBINED_SUBSCRIPTION,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        raise unittest.SkipTest(
            f"Project runtime dependency is not installed: {exc.name}"
        ) from exc
    raise

from tests.promo_test_helpers import (
    FakeSubscriptionService,
    PatchMixin,
    make_service,
)


class PromoConstraintTests(PatchMixin, unittest.IsolatedAsyncioTestCase):
    async def test_before_registration_rule_rejects_users_registered_after_cutoff(self):
        async def fake_get_user_by_id(_session, _user_id):
            return SimpleNamespace(
                registration_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )

        self.patch_attr(
            promo_service_module.user_dal,
            "get_user_by_id",
            fake_get_user_by_id,
        )
        service = make_service(FakeSubscriptionService(active_by_kind={"base": False}))
        promo = SimpleNamespace(
            code="OLDUSERS",
            min_user_registration_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            registration_date_direction="before",
            subscription_presence_mode="any",
            renewal_only=False,
        )

        error = await service._validate_promo_constraints(
            None,
            user_id=123,
            promo_data=promo,
            user_lang="ru",
            payment_kind=PAYMENT_KIND_BASE_SUBSCRIPTION,
        )

        self.assertEqual(error, "promo_code_user_registered_too_late")

    async def test_after_registration_rule_rejects_users_registered_before_cutoff(self):
        async def fake_get_user_by_id(_session, _user_id):
            return SimpleNamespace(
                registration_date=datetime(2025, 12, 31, tzinfo=timezone.utc),
            )

        self.patch_attr(
            promo_service_module.user_dal,
            "get_user_by_id",
            fake_get_user_by_id,
        )
        service = make_service(FakeSubscriptionService(active_by_kind={"base": False}))
        promo = SimpleNamespace(
            code="NEWUSERS",
            min_user_registration_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            registration_date_direction="after",
            subscription_presence_mode="any",
            renewal_only=False,
        )

        error = await service._validate_promo_constraints(
            None,
            user_id=123,
            promo_data=promo,
            user_lang="ru",
            payment_kind=PAYMENT_KIND_BASE_SUBSCRIPTION,
        )

        self.assertEqual(error, "promo_code_user_registered_too_early")

    async def test_inactive_only_rejects_user_with_active_required_subscription(self):
        async def fake_get_user_by_id(_session, _user_id):
            return SimpleNamespace(registration_date=None)

        subscription_service = FakeSubscriptionService(active_by_kind={"base": True})
        self.patch_attr(
            promo_service_module.user_dal,
            "get_user_by_id",
            fake_get_user_by_id,
        )
        service = make_service(subscription_service)
        promo = SimpleNamespace(
            code="FIRSTBUY",
            min_user_registration_date=None,
            registration_date_direction="after",
            subscription_presence_mode="inactive_only",
            renewal_only=False,
        )

        error = await service._validate_promo_constraints(
            None,
            user_id=123,
            promo_data=promo,
            user_lang="ru",
            payment_kind=PAYMENT_KIND_COMBINED_SUBSCRIPTION,
        )

        self.assertEqual(error, "promo_code_only_without_active_subscription")
        self.assertEqual(subscription_service.has_active_calls, [(123, "base")])

    async def test_active_only_for_topup_checks_addon_subscription(self):
        async def fake_get_user_by_id(_session, _user_id):
            return SimpleNamespace(registration_date=None)

        subscription_service = FakeSubscriptionService(active_by_kind={"addon": False})
        self.patch_attr(
            promo_service_module.user_dal,
            "get_user_by_id",
            fake_get_user_by_id,
        )
        service = make_service(subscription_service)
        promo = SimpleNamespace(
            code="TOPUPONLY",
            min_user_registration_date=None,
            registration_date_direction="after",
            subscription_presence_mode="active_only",
            renewal_only=False,
        )

        error = await service._validate_promo_constraints(
            None,
            user_id=123,
            promo_data=promo,
            user_lang="ru",
            payment_kind=PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
        )

        self.assertEqual(error, "promo_code_only_for_renewal")
        self.assertEqual(subscription_service.has_active_calls, [(123, "addon")])
