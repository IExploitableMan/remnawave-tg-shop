import unittest
from types import SimpleNamespace

try:
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


class PromoApplicabilityTests(unittest.TestCase):
    def test_promo_service_checks_explicit_payment_kind_flags(self):
        promo = SimpleNamespace(
            applies_to_base_subscription=True,
            applies_to_combined_subscription=False,
            applies_to_addon_subscription=True,
            applies_to_addon_traffic_topup=False,
        )

        self.assertTrue(
            PromoCodeService._promo_applies_to_payment_kind(
                promo,
                PAYMENT_KIND_BASE_SUBSCRIPTION,
            )
        )
        self.assertFalse(
            PromoCodeService._promo_applies_to_payment_kind(
                promo,
                PAYMENT_KIND_COMBINED_SUBSCRIPTION,
            )
        )
        self.assertTrue(
            PromoCodeService._promo_applies_to_payment_kind(
                promo,
                PAYMENT_KIND_ADDON_SUBSCRIPTION,
            )
        )
        self.assertFalse(
            PromoCodeService._promo_applies_to_payment_kind(
                promo,
                PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
            )
        )

    def test_dal_maps_payment_kinds_to_matching_columns(self):
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(
                PAYMENT_KIND_BASE_SUBSCRIPTION
            ).key,
            "applies_to_base_subscription",
        )
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(
                PAYMENT_KIND_COMBINED_SUBSCRIPTION
            ).key,
            "applies_to_combined_subscription",
        )
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(
                PAYMENT_KIND_ADDON_SUBSCRIPTION
            ).key,
            "applies_to_addon_subscription",
        )
        self.assertEqual(
            promo_code_dal._applicability_column_for_payment_kind(
                PAYMENT_KIND_ADDON_TRAFFIC_TOPUP
            ).key,
            "applies_to_addon_traffic_topup",
        )
