"""Засчитанные рефералы конкурса (ТЗ 5.6, 9.3).

Ключевое правило: друг, пришедший по ссылке, приносит билеты пригласившему
**только после того**, как сам выполнит условия конкурса (подпишется на каналы,
введёт кодовое слово, выполнит задание спонсора). Если у конкурса условий нет
вовсе, то само участие и есть действие.

Засчитанный реферал фиксируется в `contest_referrals` (уникально по
`(contest_id, referred_id)`), а билеты пригласившему начисляются через
`ticket_service` (идемпотентно). Так двойное зачисление невозможно.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contest, ContestEntry, ContestReferral, Referral
from app.services import contest_settings as cset
from app.services import leaderboard_service as lb
from app.services import ticket_service as tickets

logger = logging.getLogger("fable.contest_referral")


async def get_inviter(session: AsyncSession, referred_id: int) -> int | None:
    """Возвращает пригласившего для пользователя (по глобальной реферальной связи)."""
    inviter_id = await session.scalar(
        select(Referral.inviter_id).where(Referral.invited_id == referred_id)
    )
    if inviter_id is not None:
        return int(inviter_id)
    return None


def contest_has_conditions(settings: dict) -> bool:
    """Есть ли у конкурса условия, которые приглашённый обязан выполнить."""
    return bool(settings.get("code_words") or settings.get("sponsor_tasks"))


def is_referral_action_done(
    contest: Contest, entry: ContestEntry, settings: dict, *, has_channels: bool
) -> bool:
    """Выполнил ли приглашённый действие, достаточное для зачёта реферала.

    Если у конкурса нет условий (ни каналов, ни кодовых слов, ни заданий), то
    участие само по себе является действием. Иначе требуется флаг conditions_met,
    который выставляется после прохождения подписки и заданий.
    """
    if entry.status != "active":
        return False
    if not has_channels and not contest_has_conditions(settings):
        return True
    return bool(entry.conditions_met)


async def credit_referral(
    session: AsyncSession, *, contest: Contest, referred_entry: ContestEntry
) -> bool:
    """Зачитывает один реферал и начисляет билеты пригласившему. Идемпотентно."""
    referred_id = referred_entry.user_id
    inviter_id = await get_inviter(session, referred_id)
    if not inviter_id or inviter_id == referred_id:
        return False

    # Уже зачтён — не начисляем повторно.
    existing = await session.scalar(
        select(ContestReferral.id).where(
            ContestReferral.contest_id == contest.id,
            ContestReferral.referred_id == referred_id,
        )
    )
    if existing is not None:
        return False

    # Пригласивший должен сам участвовать в этом конкурсе, иначе билеты некуда
    # начислить (при участии позже реферал зачтётся на следующем проходе).
    inviter_entry = await tickets.get_entry(session, contest.id, inviter_id)
    if inviter_entry is None or inviter_entry.status != "active":
        return False

    settings = cset.read_settings(contest)
    per_ref = max(1, int(settings.get("tickets_per_ref", 1) or 1))

    try:
        async with session.begin_nested():
            session.add(
                ContestReferral(
                    contest_id=contest.id,
                    referrer_id=inviter_id,
                    referred_id=referred_id,
                    tickets_awarded=per_ref,
                )
            )
            await session.flush()
    except Exception as e:  # noqa: BLE001 — уникальность referred_id: уже зачтён
        logger.info("Реферал конкурса уже зачтён (%s, %s): %s", contest.id, referred_id, e)
        return False

    await tickets.award_tickets(
        session,
        contest_id=contest.id,
        user_id=inviter_id,
        amount=per_ref,
        reason=tickets.REASON_REFERRAL,
        reason_key=f"referral:{referred_id}",
    )
    await lb.increment_invited(session, contest_id=contest.id, user_id=inviter_id)
    logger.info(
        "Реферал зачтён: игрок %s привёл %s (+%s билетов) в конкурсе %s",
        inviter_id, referred_id, per_ref, contest.id,
    )
    return True


async def credit_referral_for_user(session: AsyncSession, *, contest_id: int, user_id: int) -> bool:
    """Пытается зачесть реферал для конкретного участника (если действие выполнено)."""
    contest = await session.get(Contest, contest_id)
    if contest is None:
        return False
    entry = await tickets.get_entry(session, contest_id, user_id)
    if entry is None:
        return False
    from app.services import contest_service as cs

    channels = await cs.get_contest_channels(session, contest_id)
    settings = cset.read_settings(contest)
    if not is_referral_action_done(contest, entry, settings, has_channels=bool(channels)):
        return False
    return await credit_referral(session, contest=contest, referred_entry=entry)


async def process_contest_referrals(session: AsyncSession, contest_id: int) -> int:
    """Обрабатывает все незачтённые рефералы конкурса. Возвращает число зачётов."""
    contest = await session.get(Contest, contest_id)
    if contest is None:
        return 0

    from app.services import contest_service as cs

    channels = await cs.get_contest_channels(session, contest_id)
    settings = cset.read_settings(contest)

    entries = (
        await session.scalars(
            select(ContestEntry).where(
                ContestEntry.contest_id == contest_id, ContestEntry.status == "active"
            )
        )
    ).all()

    credited = 0
    for entry in entries:
        if not is_referral_action_done(contest, entry, settings, has_channels=bool(channels)):
            continue
        if await credit_referral(session, contest=contest, referred_entry=entry):
            credited += 1
    return credited


async def mark_conditions_met(session: AsyncSession, *, contest_id: int, user_id: int) -> bool:
    """Отмечает, что участник выполнил условия конкурса (для зачёта реферала)."""
    from datetime import datetime, timezone

    entry = await tickets.get_entry(session, contest_id, user_id)
    if entry is None:
        return False
    if not entry.conditions_met:
        entry.conditions_met = True
        entry.conditions_met_at = datetime.now(timezone.utc)
        await session.flush()
    return True


async def count_referrals_for(session: AsyncSession, contest_id: int, user_id: int) -> int:
    """Сколько рефералов зачтено конкретному пригласившему в конкурсе."""
    from sqlalchemy import func

    return int(
        await session.scalar(
            select(func.count(ContestReferral.id)).where(
                ContestReferral.contest_id == contest_id,
                ContestReferral.referrer_id == user_id,
            )
        )
        or 0
    )
