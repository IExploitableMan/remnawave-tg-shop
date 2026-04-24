import unittest
from types import SimpleNamespace

try:
    from bot.services.promo_code_service import PromoCodeService
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
