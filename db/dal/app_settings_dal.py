from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AppSetting


async def get_setting(session: AsyncSession, key: str) -> Optional[AppSetting]:
    return await session.get(AppSetting, key)


async def get_settings_map(session: AsyncSession) -> Dict[str, str]:
    result = await session.execute(select(AppSetting))
    return {item.key: item.value for item in result.scalars().all()}


async def upsert_setting(
    session: AsyncSession,
    key: str,
    value: str,
    updated_by: Optional[int] = None,
) -> AppSetting:
    item = await session.get(AppSetting, key)
    if item:
        item.value = str(value)
        item.updated_by = updated_by
    else:
        item = AppSetting(key=key, value=str(value), updated_by=updated_by)
        session.add(item)
    await session.flush()
    await session.refresh(item)
    return item
