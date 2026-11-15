"""Билеты Fable Randomizer: атомарное и идемпотентное начисление (ТЗ 5.3, 9.3).

Каждый участник получает 1 базовый билет. Дополнительные билеты начисляются за
кодовые слова, задания спонсоров и приведённых друзей, которые выполнили условия.

Источник истины — таблица `ticket_logs` с уникальным ключом
`(contest_id, user_id, reason_key)`. Повторная обработка одного события не
удваивает билеты. Поле `entries.tickets` — материализованная сумма леджера,
используется алгоритмом розыгрыша как вес участника.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contest, ContestEntry, TicketLog
from app.services import contest_settings as cset

logger = logging.getLogger("fable.ticket")

# Причины начисления.
REASON_BASE = "base"
REASON_REFERRAL = "referral"
REASON_CODE_WORD = "code_word"
REASON_SPONSOR_TASK = "sponsor_task"
REASON_MANUAL = "manual"

BASE_TICKETS = 1


@dataclass
class CodeWordResult:
    status: str  # ok | disabled | not_found | already | no_entry
    tickets: int = 0
    word: str = ""


async def get_entry(session: AsyncSession, contest_id: int, user_id: int) -> ContestEntry | None:
    return await session.scalar(
        select(ContestEntry).where(
            ContestEntry.contest_id == contest_id, ContestEntry.user_id == user_id
        )
    )


async def recompute_tickets(session: AsyncSession, contest_id: int, user_id: int) -> int:
    """Пересчитывает `entries.tickets` как сумму леджера (минимум — 1 базовый)."""
    entry = await get_entry(session, contest_id, user_id)
    if entry is None:
        return 0
    total = int(
        await session.scalar(
            select(func.coalesce(func.sum(TicketLog.amount), 0)).where(
                TicketLog.contest_id == contest_id, TicketLog.user_id == user_id
            )
        )
        or 0
    )
    entry.tickets = max(BASE_TICKETS, total)
    await session.flush()
    return entry.tickets


async def award_tickets(
    session: AsyncSession,
    *,
    contest_id: int,
    user_id: int,
    amount: int,
    reason: str,
    reason_key: str,
) -> bool:
    """Начисляет билеты с защитой от повторов. Возвращает True, если начислено.

    Запись в леджер и пересчёт материализованного значения выполняются в одной
    транзакции (session_scope). Уникальное ограничение `uq_ticket_log_event`
    гарантирует, что один и тот же источник не начислит билеты дважды.
    """
    if amount <= 0:
        return False
    entry = await get_entry(session, contest_id, user_id)
    if entry is None:
        return False

    try:
        async with session.begin_nested():
            session.add(
                TicketLog(
                    contest_id=contest_id,
                    user_id=user_id,
                    amount=int(amount),
                    reason=reason,
                    reason_key=reason_key,
                )
            )
            await session.flush()
    except Exception as e:  # noqa: BLE001 — уникальный ключ события: уже начислено
        logger.info("Билеты уже начислены (%s, %s): %s", contest_id, reason_key, e)
        return False

    await recompute_tickets(session, contest_id, user_id)
    logger.info(
        "Начислено %s билет(ов) игроку %s в конкурсе %s (%s)",
        amount, user_id, contest_id, reason,
    )
    return True


async def ensure_base_ticket(session: AsyncSession, *, contest_id: int, user_id: int) -> int:
    """Гарантирует базовый билет участника. Возвращает текущее число билетов."""
    await award_tickets(
        session,
        contest_id=contest_id,
        user_id=user_id,
        amount=BASE_TICKETS,
        reason=REASON_BASE,
        reason_key=REASON_BASE,
    )
    return await recompute_tickets(session, contest_id, user_id)


def _match_code_word(settings: dict, text: str) -> dict | None:
    normalized = (text or "").strip().casefold()
    if not normalized:
        return None
    for item in settings.get("code_words_list", []):
        if str(item.get("word", "")).strip().casefold() == normalized:
            return item
    return None


async def redeem_code_word(
    session: AsyncSession, *, contest_id: int, user_id: int, text: str
) -> CodeWordResult:
    """Проверяет кодовое слово и начисляет билеты участнику конкурса."""
    contest = await session.get(Contest, contest_id)
    if contest is None:
        return CodeWordResult(status="not_found")
    settings = cset.read_settings(contest)
    if not settings.get("code_words"):
        return CodeWordResult(status="disabled")

    entry = await get_entry(session, contest_id, user_id)
    if entry is None or entry.status != "active":
        return CodeWordResult(status="no_entry")

    matched = _match_code_word(settings, text)
    if matched is None:
        return CodeWordResult(status="not_found")

    if not settings.get("code_words_multi", True):
        # Разрешено только одно кодовое слово — проверяем, не вводил ли уже другое.
        existing = await session.scalar(
            select(TicketLog.id).where(
                TicketLog.contest_id == contest_id,
                TicketLog.user_id == user_id,
                TicketLog.reason == REASON_CODE_WORD,
            )
        )
        if existing is not None:
            return CodeWordResult(status="already")

    word = str(matched.get("word", "")).strip()
    tickets = int(matched.get("tickets", 1) or 1)
    awarded = await award_tickets(
        session,
        contest_id=contest_id,
        user_id=user_id,
        amount=tickets,
        reason=REASON_CODE_WORD,
        reason_key=f"code_word:{word.casefold()}",
    )
    if not awarded:
        return CodeWordResult(status="already", tickets=tickets, word=word)
    return CodeWordResult(status="ok", tickets=tickets, word=word)


async def submit_sponsor_task(session: AsyncSession, *, contest_id: int, user_id: int) -> bool:
    """Зачитывает задание спонсора и начисляет билеты (однократно)."""
    contest = await session.get(Contest, contest_id)
    if contest is None:
        return False
    settings = cset.read_settings(contest)
    if not settings.get("sponsor_tasks"):
        return False
    return await award_tickets(
        session,
        contest_id=contest_id,
        user_id=user_id,
        amount=int(settings.get("sponsor_task_tickets", 1) or 1),
        reason=REASON_SPONSOR_TASK,
        reason_key=REASON_SPONSOR_TASK,
    )


async def get_ticket_breakdown(session: AsyncSession, contest_id: int, user_id: int) -> dict[str, int]:
    """Разбивка билетов участника по источникам (для отображения)."""
    rows = (
        await session.execute(
            select(TicketLog.reason, func.coalesce(func.sum(TicketLog.amount), 0))
            .where(TicketLog.contest_id == contest_id, TicketLog.user_id == user_id)
            .group_by(TicketLog.reason)
        )
    ).all()
    return {str(reason): int(total) for reason, total in rows}
