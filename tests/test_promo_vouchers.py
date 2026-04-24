import unittest
from types import SimpleNamespace

try:
    from bot.services import promo_code_service as promo_service_module
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        raise unittest.SkipTest(
            f"Project runtime dependency is not installed: {exc.name}"
        ) from exc
    raise

from tests.promo_test_helpers import (
    FakeSubscriptionService,
    NoopNotificationService,
    PatchMixin,
    make_service,
)


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

        self.patch_attr(
            promo_service_module.promo_code_dal,
            "get_promo_code_by_code",
            fake_get_promo_code_by_code,
        )
        self.patch_attr(
            promo_service_module.promo_code_dal,
            "get_user_activation_for_promo",
            fake_get_user_activation_for_promo,
        )
        self.patch_attr(
            promo_service_module.promo_code_dal,
            "record_promo_activation",
            fake_record_promo_activation,
        )
        self.patch_attr(
            promo_service_module.promo_code_dal,
            "increment_promo_code_usage",
            fake_increment_promo_code_usage,
        )
        self.patch_attr(
            promo_service_module.user_dal,
            "get_user_by_id",
            fake_get_user_by_id,
        )
        self.patch_attr(
            promo_service_module,
            "NotificationService",
            NoopNotificationService,
        )

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

        self.patch_attr(
            promo_service_module.promo_code_dal,
            "get_promo_code_by_code",
            fake_get_promo_code_by_code,
        )
        self.patch_attr(
            promo_service_module.user_dal,
            "get_user_by_id",
            fake_get_user_by_id,
        )

        service = make_service(FakeSubscriptionService(active_by_kind={"addon": False}))

        success, result = await service.apply_traffic_voucher_code(
            None,
            user_id=123,
            code_input="GIFT5",
            user_lang="ru",
        )

        self.assertFalse(success)
        self.assertEqual(result, "promo_code_only_for_renewal")
