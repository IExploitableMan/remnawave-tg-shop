import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import select, update, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AddonTrafficTopUp


ACTIVE_TOPUP_STATUSES = ("active", "partially_used")


async def create_topup(
    session: AsyncSession,
    payload: Dict[str, Any],
) -> AddonTrafficTopUp:
    topup = AddonTrafficTopUp(**payload)
    session.add(topup)
    await session.flush()
    await session.refresh(topup)
    return topup


async def get_topup_by_payment_id(
    session: AsyncSession,
    payment_id: int,
) -> Optional[AddonTrafficTopUp]:
    stmt = select(AddonTrafficTopUp).where(AddonTrafficTopUp.payment_id == payment_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_active_topups_for_subscription(
    session: AsyncSession,
    subscription_id: int,
    now: Optional[datetime] = None,
) -> List[AddonTrafficTopUp]:
    now_utc = now or datetime.now(timezone.utc)
    stmt = (
        select(AddonTrafficTopUp)
        .where(
            AddonTrafficTopUp.subscription_id == subscription_id,
            AddonTrafficTopUp.status.in_(ACTIVE_TOPUP_STATUSES),
            AddonTrafficTopUp.remaining_bytes > 0,
            AddonTrafficTopUp.expires_at > now_utc,
        )
        .order_by(AddonTrafficTopUp.created_at.asc(), AddonTrafficTopUp.topup_id.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_nonexpired_topups_for_subscription(
    session: AsyncSession,
    subscription_id: int,
    now: Optional[datetime] = None,
) -> List[AddonTrafficTopUp]:
    now_utc = now or datetime.now(timezone.utc)
    stmt = (
        select(AddonTrafficTopUp)
        .where(
            AddonTrafficTopUp.subscription_id == subscription_id,
            AddonTrafficTopUp.expires_at > now_utc,
            AddonTrafficTopUp.status != "expired",
        )
        .order_by(AddonTrafficTopUp.created_at.asc(), AddonTrafficTopUp.topup_id.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_total_active_topup_remaining_bytes(
    session: AsyncSession,
    subscription_id: int,
    now: Optional[datetime] = None,
) -> int:
    now_utc = now or datetime.now(timezone.utc)
    stmt = select(func.sum(AddonTrafficTopUp.remaining_bytes)).where(
        AddonTrafficTopUp.subscription_id == subscription_id,
        AddonTrafficTopUp.status.in_(ACTIVE_TOPUP_STATUSES),
        AddonTrafficTopUp.remaining_bytes > 0,
        AddonTrafficTopUp.expires_at > now_utc,
    )
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def extend_topups_expiration(
    session: AsyncSession,
    subscription_id: int,
    new_expires_at: datetime,
) -> int:
    stmt = (
        update(AddonTrafficTopUp)
        .where(
            AddonTrafficTopUp.subscription_id == subscription_id,
            AddonTrafficTopUp.status.in_(ACTIVE_TOPUP_STATUSES),
        )
        .values(expires_at=new_expires_at)
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def expire_topups_for_subscription(
    session: AsyncSession,
    subscription_id: int,
) -> int:
    stmt = (
        update(AddonTrafficTopUp)
        .where(
            AddonTrafficTopUp.subscription_id == subscription_id,
            AddonTrafficTopUp.status.in_(ACTIVE_TOPUP_STATUSES),
        )
        .values(status="expired", remaining_bytes=0)
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def consume_topup_bytes_fifo(
    session: AsyncSession,
    subscription_id: int,
    bytes_to_consume: int,
    now: Optional[datetime] = None,
) -> int:
    if bytes_to_consume <= 0:
        return 0

    remaining = int(bytes_to_consume)
    consumed = 0
    topups = await get_active_topups_for_subscription(session, subscription_id, now=now)
    for topup in topups:
        if remaining <= 0:
            break
        available = int(topup.remaining_bytes or 0)
        if available <= 0:
            continue
        used_now = min(available, remaining)
        topup.remaining_bytes = available - used_now
        if topup.remaining_bytes <= 0:
            topup.remaining_bytes = 0
            topup.status = "exhausted"
        else:
            topup.status = "partially_used"
        remaining -= used_now
        consumed += used_now

    if consumed:
        await session.flush()
    return consumed


async def mark_expired_topups(
    session: AsyncSession,
    now: Optional[datetime] = None,
) -> int:
    now_utc = now or datetime.now(timezone.utc)
    stmt = (
        update(AddonTrafficTopUp)
        .where(
            AddonTrafficTopUp.status.in_(ACTIVE_TOPUP_STATUSES),
            AddonTrafficTopUp.expires_at <= now_utc,
        )
        .values(status="expired", remaining_bytes=0)
    )
    result = await session.execute(stmt)
    updated = result.rowcount or 0
    if updated:
        logging.info("Expired %s add-on top-up packages.", updated)
    return updated
