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

import hashlib
import logging
import random
import secrets
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Contest,
    ContestEntry,
    DrawLog,
    Player,
    RandomizerHistory,
    Winner,
)
from app.services import contest_settings as cset
from app.services.randomizer_settings import get_max_referral_bonus_units

logger = logging.getLogger("fable.draw")

ALGORITHM_VERSION = "1.3"
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
    reserves: list[int] = field(default_factory=list)
    seed: str = ""
    salt: str = ""
    participants_hash: str = ""
    log_id: int | None = None


def compute_participants_hash(participants: list[int], weights: dict[int, int], salt: str) -> str:
    """SHA-256 хэш списка участников с их билетами и солью (ТЗ 7.2).

    Любой может воспроизвести хэш из опубликованного списка участников и соли и
    убедиться, что список не менялся после розыгрыша.
    """
    parts = [salt]
    for uid in participants:
        parts.append(f"{uid}:{int(weights.get(uid, 0))}")
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    rng = random.Random(_seed_to_int(log.seed))
    return _weighted_sample(participants, weights, int(log.winners_count or 0), rng)


def _seed_to_int(seed: str | None) -> int:
    """Преобразует сохранённый seed в целое для детерминированной проверки.

    Seed хранится как десятичная строка целого числа, чтобы `random.Random`
    воспроизводил выбор одинаково в любом процессе (строковый seed в Python
    зависит от PYTHONHASHSEED и не годится для внешней проверки честности).
    """
    raw = (seed or "").strip()
    if raw.isdigit():
        return int(raw)
    if raw.startswith("0x"):
        try:
            return int(raw, 16)
        except ValueError:
            pass
    # Совместимость со старыми логами (строковый seed) — как есть.
    return hash(raw)


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
        # Вес = билеты (1 билет = 1 базовый вес) плюс реферальный бонус в юнитах.
        # У каждого участника минимум 1 билет, поэтому базовый вес ненулевой.
        ticket_weight = BASE_WEIGHT * max(1, int(entry.tickets or 1))
        bonus = max(0, min(int(entry.bonus_weight or 0), max_units))
        participants.append(entry.user_id)
        weights[entry.user_id] = ticket_weight + bonus

    # Номер раунда = (максимум по логам) + 1.
    prev_round = int(
        await session.scalar(
            select(func.coalesce(func.max(DrawLog.round), 0)).where(DrawLog.contest_id == contest_id)
        )
        or 0
    )
    draw_round = prev_round + 1

    # Числовой seed (десятичная строка) — воспроизводится внешним верификатором
    # независимо от процесса. Соль — для проверки хэша списка участников.
    seed = str(secrets.randbits(64))
    salt = secrets.token_hex(16)
    rng = random.Random(int(seed))

    target = winners_count if winners_count is not None else contest.winners_count
    target = max(1, int(target))
    if target > len(participants):
        logger.warning(
            "Запрошено %s победителей, но участников только %s — будет выбрано %s",
            target, len(participants), len(participants),
        )

    settings = cset.read_settings(contest)
    reserve_target = max(0, int(settings.get("reserve_winners", 0) or 0))

    # Выбираем основных и запасных победителей из одного пула без повторов.
    total_pick = min(target + reserve_target, len(participants))
    picked = _weighted_sample(participants, weights, total_pick, rng)
    winner_ids = picked[:target]
    reserve_ids = picked[target:]

    from datetime import datetime, timezone

    notified_at = datetime.now(timezone.utc)
    for place, uid in enumerate(winner_ids, start=1):
        session.add(
            Winner(
                contest_id=contest_id,
                user_id=uid,
                place=place,
                prize=contest.prize,
                draw_round=draw_round,
                is_reserve=False,
                notified_at=notified_at,
            )
        )
    for place, uid in enumerate(reserve_ids, start=1):
        session.add(
            Winner(
                contest_id=contest_id,
                user_id=uid,
                place=place,
                prize=contest.prize,
                draw_round=draw_round,
                is_reserve=True,
            )
        )

    # Плоская история выпадений — переживает удаление конкурса.
    prize = (contest.prize or "").strip()
    for place, uid in enumerate(winner_ids, start=1):
        session.add(
            RandomizerHistory(
                user_id=uid,
                contest_id=contest_id,
                result=_history_result(place, prize, reserve=False),
            )
        )
    for place, uid in enumerate(reserve_ids, start=1):
        session.add(
            RandomizerHistory(
                user_id=uid,
                contest_id=contest_id,
                result=_history_result(place, prize, reserve=True),
                is_reserve=True,
            )
        )

    participants_hash = compute_participants_hash(participants, weights, salt)
    log = DrawLog(
        contest_id=contest_id,
        round=draw_round,
        algorithm_version=ALGORITHM_VERSION,
        seed=seed,
        salt=salt,
        participants_hash=participants_hash,
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
        "Розыгрыш конкурса %s (раунд %s): %s победителей, %s запасных, из %s участников",
        contest_id, draw_round, len(winner_ids), len(reserve_ids), len(participants),
    )
    return DrawResult(
        contest_id=contest_id,
        round=draw_round,
        participants=participants,
        weights=weights,
        winners=winner_ids,
        reserves=reserve_ids,
        seed=seed,
        salt=salt,
        participants_hash=participants_hash,
        log_id=log.id,
    )


async def _latest_round(session: AsyncSession, contest_id: int) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(func.max(Winner.draw_round), 0)).where(Winner.contest_id == contest_id)
        )
        or 0
    )


async def get_latest_winners(session: AsyncSession, contest_id: int) -> list[tuple[Winner, Player | None]]:
    """Основные победители последнего раунда (без запасных)."""
    latest_round = await _latest_round(session, contest_id)
    if not latest_round:
        return []
    winners = (
        await session.scalars(
            select(Winner)
            .where(
                Winner.contest_id == contest_id,
                Winner.draw_round == latest_round,
                Winner.is_reserve.is_(False),
                Winner.status != "transferred",
            )
            .order_by(Winner.place.asc())
        )
    ).all()
    result: list[tuple[Winner, Player | None]] = []
    for w in winners:
        result.append((w, await session.get(Player, w.user_id)))
    return result


async def get_latest_reserves(session: AsyncSession, contest_id: int) -> list[tuple[Winner, Player | None]]:
    """Запасные победители последнего раунда."""
    latest_round = await _latest_round(session, contest_id)
    if not latest_round:
        return []
    rows = (
        await session.scalars(
            select(Winner)
            .where(
                Winner.contest_id == contest_id,
                Winner.draw_round == latest_round,
                Winner.is_reserve.is_(True),
            )
            .order_by(Winner.place.asc())
        )
    ).all()
    return [(w, await session.get(Player, w.user_id)) for w in rows]


async def get_pending_main_winners(session: AsyncSession, contest_id: int) -> list[Winner]:
    """Основные победители последнего раунда, ещё не подтвердившие получение приза."""
    latest_round = await _latest_round(session, contest_id)
    if not latest_round:
        return []
    rows = (
        await session.scalars(
            select(Winner).where(
                Winner.contest_id == contest_id,
                Winner.draw_round == latest_round,
                Winner.is_reserve.is_(False),
                Winner.status == "pending",
            )
        )
    ).all()
    return list(rows)


async def confirm_winner(session: AsyncSession, *, contest_id: int, user_id: int) -> bool:
    """Победитель подтверждает связь — приз остаётся у него."""
    from datetime import datetime, timezone

    winner = await session.scalar(
        select(Winner).where(Winner.contest_id == contest_id, Winner.user_id == user_id)
    )
    if winner is None or winner.status not in ("pending",):
        return False
    winner.status = "confirmed"
    winner.notified_at = winner.notified_at or datetime.now(timezone.utc)
    await session.flush()
    return True


async def transfer_to_reserve(session: AsyncSession, *, contest_id: int, main_winner_id: int) -> Winner | None:
    """Передаёт приз от основного победителя к первому свободному запасному.

    Помечает основного как `transferred` и возвращает запасного, ставшего
    основным (`is_reserve=False`, `status="pending"`).
    """
    from sqlalchemy import and_

    main = await session.scalar(
        select(Winner).where(Winner.contest_id == contest_id, Winner.user_id == main_winner_id)
    )
    if main is None or main.is_reserve:
        return None

    reserve = await session.scalar(
        select(Winner)
        .where(
            and_(
                Winner.contest_id == contest_id,
                Winner.draw_round == main.draw_round,
                Winner.is_reserve.is_(True),
                Winner.status.in_(("pending", "declined")),
            )
        )
        .order_by(Winner.place.asc())
        .limit(1)
    )
    if reserve is None:
        main.status = "transferred"
        await session.flush()
        return None

    main.status = "transferred"
    reserve.is_reserve = False
    reserve.status = "pending"
    await session.flush()
    logger.info(
        "Приз конкурса %s передан запасному %s (вместо %s)",
        contest_id, reserve.user_id, main_winner_id,
    )
    return reserve


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


async def get_randomizer_history(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    contest_id: int | None = None,
    limit: int = 50,
) -> list[RandomizerHistory]:
    """Плоская история выпадений (последние записи первыми)."""
    stmt = select(RandomizerHistory)
    if user_id is not None:
        stmt = stmt.where(RandomizerHistory.user_id == user_id)
    if contest_id is not None:
        stmt = stmt.where(RandomizerHistory.contest_id == contest_id)
    stmt = stmt.order_by(
        RandomizerHistory.created_at.desc(), RandomizerHistory.id.desc()
    ).limit(limit)
    return list((await session.scalars(stmt)).all())


def _history_result(place: int, prize: str, *, reserve: bool) -> str:
    role = "Запасной" if reserve else "Победитель"
    text = f"{role}, место {place}"
    return f"{text}: {prize}"[:255] if prize else text
