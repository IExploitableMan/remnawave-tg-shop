from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import (
    AdminServerReportPreference,
    ServerReport,
    ServerReportHost,
)


async def create_server_report(
    session: AsyncSession,
    *,
    user_id: int,
    issue_type: str,
    hosts: Iterable[dict[str, Any]],
    details: Optional[str] = None,
) -> ServerReport:
    report = ServerReport(
        user_id=user_id,
        issue_type=issue_type,
        details=details,
        status="new",
    )
    session.add(report)
    await session.flush()

    for host in hosts:
        session.add(
            ServerReportHost(
                report_id=report.report_id,
                host_uuid=str(host.get("host_uuid") or ""),
                host_name=str(host.get("host_name") or host.get("name") or "Unknown"),
                host_address=host.get("host_address") or host.get("address"),
                node_uuid=host.get("node_uuid"),
                node_name=host.get("node_name"),
                profile_kind=host.get("profile_kind"),
            )
        )

    await session.flush()
    await session.refresh(report)
    return report


async def get_last_report_by_user(
    session: AsyncSession,
    user_id: int,
) -> Optional[ServerReport]:
    stmt = (
        select(ServerReport)
        .where(ServerReport.user_id == user_id)
        .order_by(desc(ServerReport.created_at), desc(ServerReport.report_id))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_report_by_id(
    session: AsyncSession,
    report_id: int,
) -> Optional[ServerReport]:
    stmt = (
        select(ServerReport)
        .options(selectinload(ServerReport.hosts), selectinload(ServerReport.user))
        .where(ServerReport.report_id == report_id)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_recent_reports(
    session: AsyncSession,
    *,
    limit: int = 10,
    offset: int = 0,
) -> list[ServerReport]:
    stmt = (
        select(ServerReport)
        .options(selectinload(ServerReport.hosts), selectinload(ServerReport.user))
        .order_by(desc(ServerReport.created_at), desc(ServerReport.report_id))
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_reports(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(ServerReport.report_id)))
    return int(result.scalar_one() or 0)


async def get_report_summary(
    session: AsyncSession,
    *,
    since: Optional[datetime] = None,
) -> dict[str, Any]:
    since = since or (datetime.now(timezone.utc) - timedelta(hours=24))

    total_result = await session.execute(select(func.count(ServerReport.report_id)))
    last_24h_result = await session.execute(
        select(func.count(ServerReport.report_id)).where(ServerReport.created_at >= since)
    )
    by_issue_result = await session.execute(
        select(ServerReport.issue_type, func.count(ServerReport.report_id))
        .where(ServerReport.created_at >= since)
        .group_by(ServerReport.issue_type)
        .order_by(desc(func.count(ServerReport.report_id)))
    )
    by_host_result = await session.execute(
        select(ServerReportHost.host_name, func.count(ServerReportHost.report_host_id))
        .join(ServerReport, ServerReport.report_id == ServerReportHost.report_id)
        .where(ServerReport.created_at >= since)
        .group_by(ServerReportHost.host_name)
        .order_by(desc(func.count(ServerReportHost.report_host_id)))
        .limit(5)
    )

    return {
        "total": int(total_result.scalar_one() or 0),
        "last_24h": int(last_24h_result.scalar_one() or 0),
        "by_issue": {issue: int(count) for issue, count in by_issue_result.all()},
        "top_hosts": [(host, int(count)) for host, count in by_host_result.all()],
    }


async def get_admin_report_preference(
    session: AsyncSession,
    admin_id: int,
) -> Optional[AdminServerReportPreference]:
    return await session.get(AdminServerReportPreference, admin_id)


async def get_admin_reports_enabled(session: AsyncSession, admin_id: int) -> bool:
    pref = await get_admin_report_preference(session, admin_id)
    return True if pref is None else bool(pref.reports_enabled)


async def set_admin_reports_enabled(
    session: AsyncSession,
    admin_id: int,
    enabled: bool,
) -> AdminServerReportPreference:
    pref = await get_admin_report_preference(session, admin_id)
    if pref:
        pref.reports_enabled = enabled
        await session.flush()
        await session.refresh(pref)
        return pref

    pref = AdminServerReportPreference(admin_id=admin_id, reports_enabled=enabled)
    session.add(pref)
    await session.flush()
    await session.refresh(pref)
    return pref


async def get_enabled_admin_ids(
    session: AsyncSession,
    admin_ids: Iterable[int],
) -> list[int]:
    ids = [int(admin_id) for admin_id in admin_ids]
    if not ids:
        return []

    result = await session.execute(
        select(AdminServerReportPreference).where(
            AdminServerReportPreference.admin_id.in_(ids)
        )
    )
    prefs = {pref.admin_id: pref.reports_enabled for pref in result.scalars().all()}
    return [admin_id for admin_id in ids if prefs.get(admin_id, True)]


async def mark_report_seen(
    session: AsyncSession,
    report_id: int,
) -> None:
    await session.execute(
        update(ServerReport)
        .where(ServerReport.report_id == report_id)
        .values(status="seen")
    )
