"""Материализованный рейтинг приглашающих (ТЗ 6.8, 9.3).

Топ приглашающих не считается при каждом просмотре: таблица
`contest_leaderboard` обновляется по событию (при зачёте реферала) и
периодически фоновым воркером. Чтение рейтинга — простой SELECT с сортировкой.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ContestLeaderboard, ContestReferral, Player

logger = logging.getLogger("fable.leaderboard")


async def increment_invited(session: AsyncSession, *, contest_id: int, user_id: int) -> None:
    """Увеличивает счётчик приглашённых у участника (по событию зачёта реферала)."""
    row = await session.scalar(
        select(ContestLeaderboard).where(
            ContestLeaderboard.contest_id == contest_id, ContestLeaderboard.user_id == user_id
        )
    )
    if row is None:
        session.add(
            ContestLeaderboard(contest_id=contest_id, user_id=user_id, invited_count=1)
        )
    else:
        row.invited_count = int(row.invited_count or 0) + 1
    await session.flush()


async def refresh_leaderboard(session: AsyncSession, contest_id: int) -> int:
    """Пересчитывает рейтинг конкурса из таблицы зачётов. Возвращает число строк."""
    rows = (
        await session.execute(
            select(ContestReferral.referrer_id, func.count(ContestReferral.id))
            .where(ContestReferral.contest_id == contest_id)
            .group_by(ContestReferral.referrer_id)
        )
    ).all()
    counts = {int(uid): int(cnt) for uid, cnt in rows}

    existing = (
        await session.scalars(
            select(ContestLeaderboard).where(ContestLeaderboard.contest_id == contest_id)
        )
    ).all()
    by_user = {int(r.user_id): r for r in existing}

    for uid, cnt in counts.items():
        row = by_user.get(uid)
        if row is None:
            session.add(ContestLeaderboard(contest_id=contest_id, user_id=uid, invited_count=cnt))
        else:
            row.invited_count = cnt
    # Обнуляем записи, потерявшие рефералов (например, после отмены участия).
    for uid, row in by_user.items():
        if uid not in counts:
            row.invited_count = 0
    await session.flush()
    return len(counts)


async def get_top(
    session: AsyncSession, contest_id: int, *, limit: int = 10
) -> list[tuple[int, int, Player | None]]:
    """Топ приглашающих: (user_id, invited_count, player)."""
    rows = (
        await session.scalars(
            select(ContestLeaderboard)
            .where(
                ContestLeaderboard.contest_id == contest_id,
                ContestLeaderboard.invited_count > 0,
            )
            .order_by(ContestLeaderboard.invited_count.desc(), ContestLeaderboard.user_id.asc())
            .limit(limit)
        )
    ).all()
    result: list[tuple[int, int, Player | None]] = []
    for row in rows:
        player = await session.get(Player, row.user_id)
        result.append((int(row.user_id), int(row.invited_count), player))
    return result


async def get_invited_count(session: AsyncSession, contest_id: int, user_id: int) -> int:
    value = await session.scalar(
        select(ContestLeaderboard.invited_count).where(
            ContestLeaderboard.contest_id == contest_id, ContestLeaderboard.user_id == user_id
        )
    )
    return int(value or 0)
