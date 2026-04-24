import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

try:
    from bot.services import promo_code_service as promo_service_module
    from bot.services.promo_code_service import PromoCodeService
    from bot.utils.product_kinds import (
        PAYMENT_KIND_ADDON_SUBSCRIPTION,
        PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
        PAYMENT_KIND_BASE_SUBSCRIPTION,
        PAYMENT_KIND_COMBINED_SUBSCRIPTION,
    )
    from db.dal import promo_code_dal
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        raise unittest.SkipTest(
            f"Project runtime dependency is not installed: {exc.name}"
        ) from exc
    raise


class FakeI18n:
    def gettext(self, _lang, key, **_kwargs):
        return key


class FakeSubscriptionService:
    def __init__(self, active_by_kind=None, voucher_activation=None):
        self.active_by_kind = active_by_kind or {}
        self.voucher_activation = voucher_activation
        self.has_active_calls = []
        self.voucher_calls = []

    async def has_active_subscription(self, _session, user_id, kind="base"):
        self.has_active_calls.append((user_id, kind))
        return bool(self.active_by_kind.get(kind, False))

    async def grant_addon_topup_via_voucher(self, **kwargs):
        self.voucher_calls.append(kwargs)
        return self.voucher_activation


class NoopNotificationService:
    def __init__(self, *_args, **_kwargs):
        pass

    async def notify_traffic_voucher_activation(self, **_kwargs):
        return None


def make_settings():
    return SimpleNamespace(
        DISCOUNT_PROMO_PAYMENT_TIMEOUT_MINUTES=10,
        subscription_options={1: 200, 3: 600},
        combined_subscription_options={1: 300, 3: 900},
        addon_subscription_options={1: 100},
        addon_traffic_packages={5.0: 100, 10.0: 180},
        stars_subscription_options={1: 150},
        combined_stars_subscription_options={1: 230},
        addon_stars_subscription_options={1: 80},
        addon_stars_traffic_packages={5.0: 75},
    )


def make_service(subscription_service=None):
    return PromoCodeService(
        settings=make_settings(),
        subscription_service=subscription_service or FakeSubscriptionService(),
        bot=None,
        i18n=FakeI18n(),
    )


class PatchMixin:
    def patch_attr(self, target, name, value):
        original = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, original)


class PromoDiscountPricingTests(unittest.TestCase):
    def test_combined_discount_defaults_to_base_component_only(self):
        service = make_service()

        details = service.calculate_discounted_offer_details(
            value=1,
            payment_kind=PAYMENT_KIND_COMBINED_SUBSCRIPTION,
            discount_percentage=10,
            combined_discount_scope="base_only",
        )

        self.assertEqual(details["original_price"], 300.0)
        self.assertEqual(details["discount_amount"], 20.0)
        self.assertEqual(details["final_price"], 280.0)
        self.assertEqual(details["discount_scope_applied"], "base_only")

    def test_combined_discount_can_apply_to_full_upgraded_price(self):
        service = make_service()

        details = service.calculate_discounted_offer_details(
            value=1,
            payment_kind=PAYMENT_KIND_COMBINED_SUBSCRIPTION,
            discount_percentage=10,
            combined_discount_scope="full",
        )

        self.assertEqual(details["original_price"], 300.0)
        self.assertEqual(details["discount_amount"], 30.0)
        self.assertEqual(details["final_price"], 270.0)
        self.assertEqual(details["discount_scope_applied"], "full")

    def test_max_discount_amount_caps_discount_and_marks_cap(self):
        service = make_service()

        details = service.calculate_discounted_offer_details(
            value=3,
            payment_kind=PAYMENT_KIND_COMBINED_SUBSCRIPTION,
            discount_percentage=50,
            max_discount_amount=200,
            combined_discount_scope="full",
        )

        self.assertEqual(details["original_price"], 900.0)
        self.assertEqual(details["discount_amount"], 200.0)
        self.assertEqual(details["final_price"], 700.0)
        self.assertTrue(details["cap_applied"])

    def test_addon_traffic_topup_uses_package_price_source(self):
        service = make_service()

        details = service.calculate_discounted_offer_details(
            value=10,
            payment_kind=PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
            discount_percentage=25,
        )

        self.assertEqual(details["original_price"], 180.0)
        self.assertEqual(details["discount_amount"], 45.0)
        self.assertEqual(details["final_price"], 135.0)


class PromoApplicabilityTests(unittest.TestCase):
    def test_promo_service_checks_explicit_payment_kind_flags(self):
        promo = SimpleNamespace(
            applies_to_base_subscription=True,
            applies_to_combined_subscription=False,
            applies_to_addon_subscription=True,
            applies_to_addon_traffic_topup=False,
        )

        self.assertTrue(PromoCodeService._promo_applies_to_payment_kind(promo, PAYMENT_KIND_BASE_SUBSCRIPTION))
        self.assertFalse(PromoCodeService._promo_applies_to_payment_kind(promo, PAYMENT_KIND_COMBINED_SUBSCRIPTION))
        self.assertTrue(PromoCodeService._promo_applies_to_payment_kind(promo, PAYMENT_KIND_ADDON_SUBSCRIPTION))
        self.assertFalse(PromoCodeService._promo_applies_to_payment_kind(promo, PAYMENT_KIND_ADDON_TRAFFIC_TOPUP))

    def test_dal_maps_payment_kinds_to_matching_columns(self):
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(PAYMENT_KIND_BASE_SUBSCRIPTION).key,
            "applies_to_base_subscription",
        )
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(PAYMENT_KIND_COMBINED_SUBSCRIPTION).key,
            "applies_to_combined_subscription",
        )
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(PAYMENT_KIND_ADDON_SUBSCRIPTION).key,
            "applies_to_addon_subscription",
        )
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(PAYMENT_KIND_ADDON_TRAFFIC_TOPUP).key,
            "applies_to_addon_traffic_topup",
        )


class PromoConstraintTests(PatchMixin, unittest.IsolatedAsyncioTestCase):
    async def test_before_registration_rule_rejects_users_registered_after_cutoff(self):
        async def fake_get_user_by_id(_session, _user_id):
            return SimpleNamespace(
                registration_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )

        self.patch_attr(promo_service_module.user_dal, "get_user_by_id", fake_get_user_by_id)
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

        self.patch_attr(promo_service_module.user_dal, "get_user_by_id", fake_get_user_by_id)
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
        self.patch_attr(promo_service_module.user_dal, "get_user_by_id", fake_get_user_by_id)
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
        self.patch_attr(promo_service_module.user_dal, "get_user_by_id", fake_get_user_by_id)
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


class TrafficVoucherTests(PatchMixin, unittest.IsolatedAsyncioTestCase):
    async def test_traffic_voucher_grants_addon_topup_and_records_activation(self):
        promo = SimpleNamespace(
            promo_code_id=77,
            code="GIFT5",
            promo_type="traffic_gb",
            is_active=True,
            current_activations=0,
            max_activations=10,
            valid_until=None,
            traffic_amount_gb=5,
            min_user_registration_date=None,
            registration_date_direction="after",
            subscription_presence_mode="active_only",
            renewal_only=False,
        )

        async def fake_get_promo_code_by_code(_session, code):
            self.assertEqual(code, "GIFT5")
            return promo

        async def fake_get_user_by_id(_session, _user_id):
            return SimpleNamespace(registration_date=None, username="tester")

        async def fake_get_user_activation_for_promo(_session, _promo_code_id, _user_id):
            return None

        async def fake_record_promo_activation(_session, promo_code_id, user_id, payment_id=None):
            self.assertEqual((promo_code_id, user_id, payment_id), (77, 123, None))
            return True

        async def fake_increment_promo_code_usage(_session, promo_code_id):
            self.assertEqual(promo_code_id, 77)
            return promo

        self.patch_attr(promo_service_module.promo_code_dal, "get_promo_code_by_code", fake_get_promo_code_by_code)
        self.patch_attr(promo_service_module.promo_code_dal, "get_user_activation_for_promo", fake_get_user_activation_for_promo)
        self.patch_attr(promo_service_module.promo_code_dal, "record_promo_activation", fake_record_promo_activation)
        self.patch_attr(promo_service_module.promo_code_dal, "increment_promo_code_usage", fake_increment_promo_code_usage)
        self.patch_attr(promo_service_module.user_dal, "get_user_by_id", fake_get_user_by_id)
        self.patch_attr(promo_service_module, "NotificationService", NoopNotificationService)

        subscription_service = FakeSubscriptionService(
            active_by_kind={"addon": True},
            voucher_activation={"traffic_remaining_bytes": 5 * 1024 ** 3},
        )
        service = make_service(subscription_service)

        success, result = await service.apply_traffic_voucher_code(
            None,
            user_id=123,
            code_input=" gift5 ",
            user_lang="ru",
        )

        self.assertTrue(success)
        self.assertEqual(result["traffic_gb"], 5.0)
        self.assertEqual(subscription_service.has_active_calls, [(123, "addon")])
        self.assertEqual(subscription_service.voucher_calls[0]["traffic_gb"], 5.0)
        self.assertEqual(subscription_service.voucher_calls[0]["promo_code"], "GIFT5")

    async def test_traffic_voucher_requires_active_addon_subscription(self):
        promo = SimpleNamespace(
            promo_code_id=77,
            code="GIFT5",
            promo_type="traffic_gb",
            is_active=True,
            current_activations=0,
            max_activations=10,
            valid_until=None,
            traffic_amount_gb=5,
            min_user_registration_date=None,
            registration_date_direction="after",
            subscription_presence_mode="active_only",
            renewal_only=False,
        )

        async def fake_get_promo_code_by_code(_session, _code):
            return promo

        async def fake_get_user_by_id(_session, _user_id):
            return SimpleNamespace(registration_date=None, username="tester")

        self.patch_attr(promo_service_module.promo_code_dal, "get_promo_code_by_code", fake_get_promo_code_by_code)
        self.patch_attr(promo_service_module.user_dal, "get_user_by_id", fake_get_user_by_id)

        service = make_service(FakeSubscriptionService(active_by_kind={"addon": False}))

        success, result = await service.apply_traffic_voucher_code(
            None,
            user_id=123,
            code_input="GIFT5",
            user_lang="ru",
        )

        self.assertFalse(success)
        self.assertEqual(result, "promo_code_only_for_renewal")
