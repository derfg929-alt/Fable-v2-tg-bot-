"""Алгоритм выбора победителей Fable Randomizer.

Правила из ТЗ:
- Отдельный список участников на каждый конкурс.
- Перед розыгрышем проверяется: игрок зарегистрирован, участвует, не заблокирован,
  выполнил условия, без дублирующих записей.
- Взвешенный случайный выбор: базовый вес ненулевой у всех, реферальный бонус
  увеличивает относительный вес, но не гарантирует победу.
- Выбирается указанное администратором число уникальных победителей (без повторов
  в рамках одного розыгрыша).
- Результаты сохраняются: участники, веса, победители, версия алгоритма, seed.
- Повторный розыгрыш сохраняет предыдущие результаты и причину повтора.
"""
from __future__ import annotations

import logging
import random
import secrets
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contest, ContestEntry, DrawLog, Player, Winner
from app.services.randomizer_settings import get_max_referral_bonus_units

logger = logging.getLogger("fable.draw")

ALGORITHM_VERSION = "1.2"
BASE_WEIGHT = 10000  # 100% в базисных пунктах


class DrawError(Exception):
    pass


@dataclass
class DrawResult:
    contest_id: int
    round: int
    participants: list[int] = field(default_factory=list)
    weights: dict[int, int] = field(default_factory=dict)
    winners: list[int] = field(default_factory=list)
    seed: str = ""
    log_id: int | None = None


async def _eligible_participants(
    session: AsyncSession,
    contest_id: int,
    *,
    exclude: set[int] | None = None,
) -> list[ContestEntry]:
    """Участники, допущенные к розыгрышу, без дубликатов.

    Проверяются условия ТЗ: игрок участвует (status=active), зарегистрирован,
    не заблокирован глобально, без дублирующих записей. Владелец конкурса
    (для пользовательских конкурсов канала) исключается из розыгрыша.
    """
    exclude = set(exclude or set())
    contest = await session.get(Contest, contest_id)
    if contest is not None and contest.owner_id:
        exclude.add(contest.owner_id)

    entries = (
        await session.scalars(
            select(ContestEntry)
            .where(ContestEntry.contest_id == contest_id, ContestEntry.status == "active")
            .order_by(ContestEntry.id.asc())
        )
    ).all()

    seen: set[int] = set()
    eligible: list[ContestEntry] = []
    for entry in entries:
        if entry.user_id in seen or entry.user_id in exclude:
            continue
        player = await session.get(Player, entry.user_id)
        if player is None or player.is_globally_banned:
            continue
        seen.add(entry.user_id)
        eligible.append(entry)
    return eligible


def _weighted_sample(
    participants: list[int], weights: dict[int, int], count: int, rng: random.Random
) -> list[int]:
    """Взвешенный выбор без повторов (каждый участник — не более одного места)."""
    pool = list(participants)
    chosen: list[int] = []
    count = min(count, len(pool))
    while len(chosen) < count and pool:
        total = sum(weights[uid] for uid in pool)
        if total <= 0:
            break
        pick = rng.uniform(0, total)
        acc = 0.0
        for uid in pool:
            acc += weights[uid]
            if acc >= pick:
                chosen.append(uid)
                pool.remove(uid)
                break
        else:  # защита от накопления ошибок с плавающей точкой
            chosen.append(pool.pop())
    return chosen


def verify_draw(log: DrawLog) -> list[int]:
    """Повторно воспроизводит розыгрыш по сохранённому seed и проверяет честность.

    Возвращает список победителей, полученный детерминированно из seed. Если он
    совпадает с сохранённым — результат подтверждён (данные для проверки честности).
    """
    participants = [int(x) for x in (log.participants or [])]
    raw_weights = log.weights or {}
    weights = {int(k): int(v) for k, v in raw_weights.items()}
    rng = random.Random(log.seed or "")
    return _weighted_sample(participants, weights, int(log.winners_count or 0), rng)


async def run_draw(
    session: AsyncSession,
    *,
    contest_id: int,
    executed_by: int | None = None,
    reason: str | None = None,
    winners_count: int | None = None,
    exclude_previous_winners: bool = False,
) -> DrawResult:
    """Проводит розыгрыш конкурса и сохраняет результаты.

    При повторном розыгрыше увеличивается номер раунда, а предыдущие результаты
    сохраняются; при `exclude_previous_winners` прошлые победители не участвуют
    в новом раунде (полезно, когда нужно выбрать других победителей).
    """
    contest = await session.get(Contest, contest_id)
    if contest is None:
        raise DrawError(f"Конкурс {contest_id} не найден")

    exclude: set[int] = set()
    if exclude_previous_winners:
        prev = (await session.scalars(select(Winner.user_id).where(Winner.contest_id == contest_id))).all()
        exclude = {int(x) for x in prev}

    entries = await _eligible_participants(session, contest_id, exclude=exclude)
    if not entries:
        raise DrawError("В конкурсе нет допущенных участников для розыгрыша")

    max_units = await get_max_referral_bonus_units(session)

    participants: list[int] = []
    weights: dict[int, int] = {}
    for entry in entries:
        bonus = max(0, min(int(entry.bonus_weight or 0), max_units))
        participants.append(entry.user_id)
        weights[entry.user_id] = BASE_WEIGHT + bonus

    # Номер раунда = (максимум по логам) + 1.
    prev_round = int(
        await session.scalar(
            select(func.coalesce(func.max(DrawLog.round), 0)).where(DrawLog.contest_id == contest_id)
        )
        or 0
    )
    draw_round = prev_round + 1

    seed = secrets.token_hex(16)
    rng = random.Random(seed)

    target = winners_count if winners_count is not None else contest.winners_count
    target = max(1, int(target))
    if target > len(participants):
        logger.warning(
            "Запрошено %s победителей, но участников только %s — будет выбрано %s",
            target, len(participants), len(participants),
        )
    winner_ids = _weighted_sample(participants, weights, target, rng)

    # Фиксируем победителей.
    for place, uid in enumerate(winner_ids, start=1):
        session.add(
            Winner(
                contest_id=contest_id,
                user_id=uid,
                place=place,
                prize=contest.prize,
                draw_round=draw_round,
            )
        )

    log = DrawLog(
        contest_id=contest_id,
        round=draw_round,
        algorithm_version=ALGORITHM_VERSION,
        seed=seed,
        participants=participants,
        weights={str(k): v for k, v in weights.items()},
        winners=winner_ids,
        winners_count=len(winner_ids),
        reason=reason,
        executed_by=executed_by,
    )
    session.add(log)

    contest.draw_done = True
    contest.status = "finished"
    await session.flush()

    logger.info(
        "Розыгрыш конкурса %s (раунд %s): %s победителей из %s участников",
        contest_id, draw_round, len(winner_ids), len(participants),
    )
    return DrawResult(
        contest_id=contest_id,
        round=draw_round,
        participants=participants,
        weights=weights,
        winners=winner_ids,
        seed=seed,
        log_id=log.id,
    )


async def get_latest_winners(session: AsyncSession, contest_id: int) -> list[tuple[Winner, Player | None]]:
    """Победители последнего раунда конкурса."""
    latest_round = await session.scalar(
        select(func.coalesce(func.max(Winner.draw_round), 0)).where(Winner.contest_id == contest_id)
    )
    if not latest_round:
        return []
    winners = (
        await session.scalars(
            select(Winner)
            .where(Winner.contest_id == contest_id, Winner.draw_round == latest_round)
            .order_by(Winner.place.asc())
        )
    ).all()
    result: list[tuple[Winner, Player | None]] = []
    for w in winners:
        result.append((w, await session.get(Player, w.user_id)))
    return result


async def get_draw_history(session: AsyncSession, contest_id: int) -> list[DrawLog]:
    return list(
        (
            await session.scalars(
                select(DrawLog)
                .where(DrawLog.contest_id == contest_id)
                .order_by(DrawLog.round.desc())
            )
        ).all()
    )
