"""Сервис топов и рейтингов Fable.

Правила из ТЗ:
- Все глобальные рейтинги единые для всех групп.
- Локальные рейтинги рассчитываются только по участникам конкретной группы.
- Топ богатых ("топ богатых", "топ богатых чат"): сортировка по общему балансу (wallet + bank_balance).
- Топ активных ("топ активных", "топ активных чат"): 50 позиций, периоды: день, неделя, месяц, всё время.
- Топ кланов ("топ кланов", "топ кланов чат"): глобальный и локальный.
- Меню "топ" с кнопками выбора категории.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRecord, Clan, ClanMember, Player


@dataclass(frozen=True, slots=True)
class TopRichEntry:
    rank: int
    user_id: int
    name: str
    total_wealth: int
    wallet: int
    bank: int


@dataclass(frozen=True, slots=True)
class TopActiveEntry:
    rank: int
    user_id: int
    name: str
    activity_points: int


@dataclass(frozen=True, slots=True)
class TopClanEntry:
    rank: int
    clan_id: int
    name: str
    level: int
    rating: int
    member_count: int


async def get_top_rich(
    session: AsyncSession,
    *,
    chat_id: int | None = None,
    limit: int = 50,
) -> list[TopRichEntry]:
    """Возвращает топ богатых игроков (глобальный или для чата)."""
    if chat_id is None:
        statement = (
            select(
                Player,
                (Player.wallet + Player.bank_balance).label("total_wealth"),
            )
            .where(Player.is_globally_banned.is_(False))
            .order_by(desc("total_wealth"))
            .limit(limit)
        )
    else:
        # Участники конкретного чата (активность в данном чате)
        subq = (
            select(ActivityRecord.user_id)
            .where(ActivityRecord.chat_id == chat_id)
            .distinct()
            .subquery()
        )
        statement = (
            select(
                Player,
                (Player.wallet + Player.bank_balance).label("total_wealth"),
            )
            .join(subq, subq.c.user_id == Player.telegram_id)
            .where(Player.is_globally_banned.is_(False))
            .order_by(desc("total_wealth"))
            .limit(limit)
        )

    res = (await session.execute(statement)).all()
    entries = []
    for rank, (player, wealth) in enumerate(res, start=1):
        entries.append(
            TopRichEntry(
                rank=rank,
                user_id=player.telegram_id,
                name=player.first_name,
                total_wealth=int(wealth),
                wallet=player.wallet,
                bank=player.bank_balance,
            )
        )
    return entries


async def get_top_active(
    session: AsyncSession,
    *,
    period: str = "all",  # 'day', 'week', 'month', 'all'
    chat_id: int | None = None,
    limit: int = 50,
) -> list[TopActiveEntry]:
    """Возвращает топ активных участников за период (глобальный или локальный)."""
    query = select(
        Player,
        func.sum(ActivityRecord.points).label("total_points"),
    ).join(Player, Player.telegram_id == ActivityRecord.user_id)

    if chat_id is not None:
        query = query.where(ActivityRecord.chat_id == chat_id)

    now = datetime.now(timezone.utc)
    if period == "day":
        query = query.where(ActivityRecord.created_at >= now - timedelta(days=1))
    elif period == "week":
        query = query.where(ActivityRecord.created_at >= now - timedelta(days=7))
    elif period == "month":
        query = query.where(ActivityRecord.created_at >= now - timedelta(days=30))

    query = (
        query.group_by(Player.telegram_id)
        .order_by(desc("total_points"))
        .limit(limit)
    )

    res = (await session.execute(query)).all()
    entries = []
    for rank, (player, points) in enumerate(res, start=1):
        entries.append(
            TopActiveEntry(
                rank=rank,
                user_id=player.telegram_id,
                name=player.first_name,
                activity_points=int(points),
            )
        )
    return entries


async def get_top_clans(
    session: AsyncSession,
    *,
    chat_id: int | None = None,
    limit: int = 50,
) -> list[TopClanEntry]:
    """Возвращает топ кланов по рейтингу."""
    if chat_id is None:
        query = (
            select(
                Clan,
                func.count(ClanMember.id).label("member_cnt"),
            )
            .outerjoin(ClanMember, ClanMember.clan_id == Clan.id)
            .where(Clan.deleted_at.is_(None))
            .group_by(Clan.id)
            .order_by(desc(Clan.rating), desc(Clan.level))
            .limit(limit)
        )
    else:
        # Кланы, участники которых писали в данном чате
        active_users_subq = (
            select(ActivityRecord.user_id)
            .where(ActivityRecord.chat_id == chat_id)
            .distinct()
            .subquery()
        )
        chat_clans_subq = (
            select(ClanMember.clan_id)
            .join(active_users_subq, active_users_subq.c.user_id == ClanMember.user_id)
            .distinct()
            .subquery()
        )
        query = (
            select(
                Clan,
                func.count(ClanMember.id).label("member_cnt"),
            )
            .join(chat_clans_subq, chat_clans_subq.c.clan_id == Clan.id)
            .outerjoin(ClanMember, ClanMember.clan_id == Clan.id)
            .where(Clan.deleted_at.is_(None))
            .group_by(Clan.id)
            .order_by(desc(Clan.rating), desc(Clan.level))
            .limit(limit)
        )

    res = (await session.execute(query)).all()
    entries = []
    for rank, (clan, cnt) in enumerate(res, start=1):
        entries.append(
            TopClanEntry(
                rank=rank,
                clan_id=clan.id,
                name=clan.name,
                level=clan.level,
                rating=clan.rating,
                member_count=int(cnt),
            )
        )
    return entries
