from typing import Optional, Dict

from config.settings import Settings
from bot.utils.product_kinds import (
    PAYMENT_KIND_ADDON_SUBSCRIPTION,
    PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
    PAYMENT_KIND_BASE_SUBSCRIPTION,
    PAYMENT_KIND_COMBINED_SUBSCRIPTION,
    normalize_payment_kind,
)


def is_traffic_payment_kind(payment_kind: str) -> bool:
    return normalize_payment_kind(payment_kind) == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP


def is_addon_payment_kind(payment_kind: str) -> bool:
    return normalize_payment_kind(payment_kind) in {
        PAYMENT_KIND_COMBINED_SUBSCRIPTION,
        PAYMENT_KIND_ADDON_SUBSCRIPTION,
        PAYMENT_KIND_ADDON_TRAFFIC_TOPUP,
    }


def get_fiat_price_source(settings: Settings, payment_kind: str) -> Dict[float, float]:
    normalized = normalize_payment_kind(payment_kind)
    if normalized == PAYMENT_KIND_COMBINED_SUBSCRIPTION:
        return {float(key): value for key, value in (settings.combined_subscription_options or {}).items()}
    if normalized == PAYMENT_KIND_ADDON_SUBSCRIPTION:
        return {float(key): value for key, value in (settings.addon_subscription_options or {}).items()}
    if normalized == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP:
        return settings.addon_traffic_packages or {}
    return {float(key): value for key, value in (settings.subscription_options or {}).items()}


def get_stars_price_source(settings: Settings, payment_kind: str) -> Dict[float, int]:
    normalized = normalize_payment_kind(payment_kind)
    if normalized == PAYMENT_KIND_COMBINED_SUBSCRIPTION:
        return {float(key): value for key, value in (settings.combined_stars_subscription_options or {}).items()}
    if normalized == PAYMENT_KIND_ADDON_SUBSCRIPTION:
        return {float(key): value for key, value in (settings.addon_stars_subscription_options or {}).items()}
    if normalized == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP:
        return settings.addon_stars_traffic_packages or {}
    return {float(key): value for key, value in (settings.stars_subscription_options or {}).items()}


def get_offer_display_value(raw_value: float, payment_kind: str) -> float:
    normalized = normalize_payment_kind(payment_kind)
    return float(raw_value)


def resolve_base_price(settings: Settings, value: float, payment_kind: str, stars: bool = False):
    price_source = get_stars_price_source(settings, payment_kind) if stars else get_fiat_price_source(settings, payment_kind)
    value_key = get_offer_display_value(value, payment_kind)
    direct = price_source.get(value_key)
    if direct is not None:
        return direct
    if float(value_key).is_integer():
        alt = price_source.get(int(value_key))  # type: ignore[arg-type]
        if alt is not None:
            return alt
    for existing_key, existing_price in price_source.items():
        if abs(float(existing_key) - float(value_key)) < 1e-9:
            return existing_price
    return None


def get_payment_description(get_text, value: float, payment_kind: str) -> str:
    normalized = normalize_payment_kind(payment_kind)
    if normalized == PAYMENT_KIND_COMBINED_SUBSCRIPTION:
        return get_text("payment_description_combined_subscription", months=int(value))
    if normalized == PAYMENT_KIND_ADDON_SUBSCRIPTION:
        return get_text("payment_description_addon_subscription")
    if normalized == PAYMENT_KIND_ADDON_TRAFFIC_TOPUP:
        human_value = str(int(value)) if float(value).is_integer() else f"{value:g}"
        return get_text("payment_description_addon_traffic", traffic_gb=human_value)
    return get_text("payment_description_subscription", months=int(value))


def get_payment_link_message_key(payment_kind: str) -> str:
    return "payment_link_message_traffic" if is_traffic_payment_kind(payment_kind) else "payment_link_message"


def get_invoice_message_key(payment_kind: str) -> str:
    return "payment_invoice_sent_message_traffic" if is_traffic_payment_kind(payment_kind) else "payment_invoice_sent_message"
