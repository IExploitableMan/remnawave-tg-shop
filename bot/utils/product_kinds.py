SUBSCRIPTION_KIND_BASE = "base"
SUBSCRIPTION_KIND_ADDON = "addon"

PAYMENT_KIND_BASE_SUBSCRIPTION = "base_subscription"
PAYMENT_KIND_COMBINED_SUBSCRIPTION = "combined_subscription"
PAYMENT_KIND_ADDON_SUBSCRIPTION = "addon_subscription"
PAYMENT_KIND_ADDON_TRAFFIC_TOPUP = "addon_traffic_topup"

ALL_PAYMENT_KINDS = {
    PAYMENT_KIND_BASE_SUBSCRIPTION,
    PAYMENT_KIND_COMBINED_SUBSCRIPTION,
    PAYMENT_KIND_ADDON_SUBSCRIPTION,
    PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
}


def normalize_payment_kind(payment_kind: str | None) -> str:
    normalized = (payment_kind or PAYMENT_KIND_BASE_SUBSCRIPTION).strip().lower()
    if normalized in {"subscription", "base"}:
        return PAYMENT_KIND_BASE_SUBSCRIPTION
    if normalized in {"combined", "combo", "upgraded", "upgraded_plan"}:
        return PAYMENT_KIND_COMBINED_SUBSCRIPTION
    if normalized in {"traffic", "addon_traffic", "addon_topup"}:
        return PAYMENT_KIND_ADDON_TRAFFIC_TOPUP
    if normalized in {"addon", "addon_month"}:
        return PAYMENT_KIND_ADDON_SUBSCRIPTION
    return normalized or PAYMENT_KIND_BASE_SUBSCRIPTION


def is_addon_subscription_kind(kind: str) -> bool:
    return (kind or "").strip().lower() == SUBSCRIPTION_KIND_ADDON


def is_addon_payment_kind(kind: str) -> bool:
    normalized = (kind or "").strip().lower()
    return normalized in {
        PAYMENT_KIND_COMBINED_SUBSCRIPTION,
        PAYMENT_KIND_ADDON_SUBSCRIPTION,
        PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
    }


def is_topup_payment_kind(kind: str) -> bool:
    return (kind or "").strip().lower() == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP


def subscription_kind_for_payment_kind(payment_kind: str) -> str:
    normalized = (payment_kind or "").strip().lower()
    if normalized in {
        PAYMENT_KIND_ADDON_SUBSCRIPTION,
        PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
    }:
        return SUBSCRIPTION_KIND_ADDON
    return SUBSCRIPTION_KIND_BASE
