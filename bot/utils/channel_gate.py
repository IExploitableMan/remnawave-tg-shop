import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import subscription_dal


CHANNEL_GATE_MODE_IMMEDIATE = "immediate"
CHANNEL_GATE_MODE_AFTER_PAID_SUBSCRIPTION = "after_paid_subscription"


async def should_enforce_channel_subscription_gate(
    settings: Settings,
    session: AsyncSession,
    user_id: int,
) -> bool:
    if not settings.REQUIRED_CHANNEL_SUBSCRIBE_TO_USE:
        return False
    if not settings.REQUIRED_CHANNEL_ID:
        return False
    if user_id in settings.ADMIN_IDS:
        return False

    mode = normalize_channel_gate_mode(
        getattr(settings, "REQUIRED_CHANNEL_SUBSCRIPTION_MODE", CHANNEL_GATE_MODE_IMMEDIATE)
    )
    if mode == CHANNEL_GATE_MODE_IMMEDIATE:
        return True

    try:
        return await subscription_dal.has_paid_subscription_for_user(session, user_id)
    except Exception as exc:
        logging.error(
            "Channel gate: failed to resolve paid subscription state for user %s: %s",
            user_id,
            exc,
            exc_info=True,
        )
        return False


def normalize_channel_gate_mode(value: Optional[str]) -> str:
    normalized = (value or CHANNEL_GATE_MODE_IMMEDIATE).strip().lower()
    if normalized in {"after_paid", "after_payment", "paid", "post_paid"}:
        return CHANNEL_GATE_MODE_AFTER_PAID_SUBSCRIPTION
    if normalized == CHANNEL_GATE_MODE_AFTER_PAID_SUBSCRIPTION:
        return CHANNEL_GATE_MODE_AFTER_PAID_SUBSCRIPTION
    return CHANNEL_GATE_MODE_IMMEDIATE
