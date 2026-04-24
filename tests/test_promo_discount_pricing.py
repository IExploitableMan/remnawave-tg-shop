import unittest

try:
    from bot.utils.product_kinds import (
        PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
        PAYMENT_KIND_COMBINED_SUBSCRIPTION,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"aiogram", "pydantic_settings", "sqlalchemy"}:
        raise unittest.SkipTest(
            f"Project runtime dependency is not installed: {exc.name}"
        ) from exc
    raise

from tests.promo_test_helpers import make_service


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
