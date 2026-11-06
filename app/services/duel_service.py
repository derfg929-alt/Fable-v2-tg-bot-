"""Групповые Дуэли Fable.

Правила из ТЗ:
- Дуэль проходит ТОЛЬКО в группе.
- Команда: "дуэль @user 100"
- Кнопки вызова: "Принять дуэль", "Отклонить дуэль".
- Если второй игрок отклоняет вызов — средства не списываются.
- Если принимает:
  1. Проверяется баланс обоих игроков.
  2. Ставки обоих игроков блокируются (списываются в резерв/депонируются).
  3. Создаётся игровая сессия (GameSession).
  4. Игроки по очереди выполняют действия.
- Кнопки во время дуэли:
  * "Прицелиться"
  * "Выстрелить"
- Механика прицеливания:
  * Каждое нажатие «Прицелиться» повышает внутренний скрытый шанс попадания.
  * Максимум 10 прицеливаний.
  * Каждое прицеливание добавляет 10 процентных пунктов.
  * Максимальный шанс попадания — 95%.
  * Скрытые проценты не показываются игрокам ни в коем случае!
  * При промахе прицеливание сбрасывается к нулю, ход переходит сопернику.
  * При успешном попадании игрок побеждает.
- Победитель забирает выигрыш (выплата = 2 * ставка), проигравший теряет ставку (ставка сжигается/переходит).
- При технической отмене ставки возвращаются обоим.
- Защита от:
  * Нажатий посторонних пользователей.
  * Повторных нажатий.
  * Подмены участников.
  * Повторного списания ставок.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import random
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GameSession, Player
from app.services.economy_service import (
    burn_coins,
    credit_coins,
    debit_coins,
    InsufficientFundsError,
    InvalidAmountError,
    PlayerNotFoundError,
)


class DuelError(Exception):
    """Базовая ошибка дуэли."""


class DuelAccessDeniedError(DuelError):
    """Действие разрешено только участникам текущей дуэли."""


class DuelNotYourTurnError(DuelError):
    """Сейчас ход другого игрока."""


@dataclass(frozen=True, slots=True)
class DuelActionResponse:
    session_id: int
    current_turn_user_id: int
    opponent_user_id: int
    action: str  # 'aim', 'shot', 'accept', 'decline'
    is_finished: bool
    winner_id: int | None
    aim_count: int
    message: str


async def create_duel_invite(
    session: AsyncSession,
    *,
    chat_id: int,
    challenger_id: int,
    opponent_id: int,
    stake: int,
) -> GameSession:
    """Создает приглашение на дуэль в группе."""
    if challenger_id == opponent_id:
        raise DuelError("Нельзя вызвать на дуэль самого себя")
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")

    challenger = await session.scalar(
        select(Player).where(Player.telegram_id == challenger_id).with_for_update()
    )
    if not challenger:
        raise PlayerNotFoundError("Вы не зарегистрированы в игре")
    if challenger.wallet < stake:
        raise InsufficientFundsError(f"Недостаточно монет для ставки: у вас {challenger.wallet}, требуется {stake}")

    opponent = await session.scalar(
        select(Player).where(Player.telegram_id == opponent_id).with_for_update()
    )
    if not opponent:
        raise PlayerNotFoundError("Вызываемый игрок не найден в системе Fable")
    if opponent.wallet < stake:
        raise InsufficientFundsError(f"У соперника недостаточно монет для дуэли (требуется {stake})")

    duel_session = GameSession(
        game_type="duel",
        status="pending",
        chat_id=chat_id,
        user_id=challenger_id,
        opponent_id=opponent_id,
        stake=stake,
        payout=0,
        state={
            "aim_counts": {str(challenger_id): 0, str(opponent_id): 0},
            "current_turn": challenger_id,
        },
    )
    session.add(duel_session)
    await session.flush()
    return duel_session


async def decline_duel(
    session: AsyncSession,
    *,
    session_id: int,
    actor_id: int,
) -> DuelActionResponse:
    """Отклонение дуэли оппонентом или отмена инициатором."""
    duel = await session.scalar(
        select(GameSession).where(GameSession.id == session_id).with_for_update()
    )
    if not duel or duel.game_type != "duel":
        raise DuelError("Дуэль не найдена")

    if duel.status != "pending":
        raise DuelError("Эта дуэль уже началась или завершена")

    if actor_id not in (duel.user_id, duel.opponent_id):
        raise DuelAccessDeniedError("Вы не участвуете в этой дуэли")

    duel.status = "cancelled"
    duel.result = f"cancelled_by_{actor_id}"
    duel.finished_at = datetime.now(timezone.utc)
    await session.flush()

    return DuelActionResponse(
        session_id=duel.id,
        current_turn_user_id=duel.user_id,
        opponent_user_id=duel.opponent_id,
        action="decline",
        is_finished=True,
        winner_id=None,
        aim_count=0,
        message="Вызов на дуэль был отклонен.",
    )


async def accept_duel(
    session: AsyncSession,
    *,
    session_id: int,
    actor_id: int,
) -> DuelActionResponse:
    """Принятие дуэли оппонентом и блокировка ставок."""
    duel = await session.scalar(
        select(GameSession).where(GameSession.id == session_id).with_for_update()
    )
    if not duel or duel.game_type != "duel":
        raise DuelError("Дуэль не найдена")

    if duel.status != "pending":
        raise DuelError("Дуэль уже принята или завершена")

    if actor_id != duel.opponent_id:
        raise DuelAccessDeniedError("Только вызванный игрок может принять дуэль!")

    # Блокируем строки игроков в строгом порядке ID
    p_ids = sorted([duel.user_id, duel.opponent_id])
    players_res = (
        await session.scalars(
            select(Player).where(Player.telegram_id.in_(p_ids)).order_by(Player.telegram_id).with_for_update()
        )
    ).all()
    p_map = {p.telegram_id: p for p in players_res}

    challenger = p_map.get(duel.user_id)
    opponent = p_map.get(duel.opponent_id)

    if not challenger or challenger.wallet < duel.stake:
        duel.status = "cancelled"
        raise InsufficientFundsError("У создателя дуэли больше нет достаточного количества монет")
    if not opponent or opponent.wallet < duel.stake:
        duel.status = "cancelled"
        raise InsufficientFundsError("У вас недостаточно монет для принятия дуэли")

    # Списываем ставки обоих игроков
    await debit_coins(
        session,
        user_id=challenger.telegram_id,
        amount=duel.stake,
        reason=f"Ставка на дуэль #{duel.id}",
        operation_type="duel_stake",
        reference=f"duel:{duel.id}",
    )
    await debit_coins(
        session,
        user_id=opponent.telegram_id,
        amount=duel.stake,
        reason=f"Ставка на дуэль #{duel.id}",
        operation_type="duel_stake",
        reference=f"duel:{duel.id}",
    )

    duel.status = "active"
    # Случайно выбираем, чей первый ход
    first_turn = random.choice([duel.user_id, duel.opponent_id])
    state = dict(duel.state or {})
    state["current_turn"] = first_turn
    state["aim_counts"] = {str(duel.user_id): 0, str(duel.opponent_id): 0}
    duel.state = state

    await session.flush()
    return DuelActionResponse(
        session_id=duel.id,
        current_turn_user_id=first_turn,
        opponent_user_id=duel.opponent_id if first_turn == duel.user_id else duel.user_id,
        action="accept",
        is_finished=False,
        winner_id=None,
        aim_count=0,
        message=f"Дуэль началась! Первый ход делает игрок ID {first_turn}.",
    )


async def duel_aim(
    session: AsyncSession,
    *,
    session_id: int,
    actor_id: int,
) -> DuelActionResponse:
    """Прицеливание: увеличивает скрытый шанс попадания (+10%, макс 10 прицеливаний). Ход переходит оппоненту."""
    duel = await session.scalar(
        select(GameSession).where(GameSession.id == session_id).with_for_update()
    )
    if not duel or duel.status != "active":
        raise DuelError("Дуэль не активна")

    state = dict(duel.state or {})
    current_turn = state.get("current_turn")
    if actor_id != current_turn:
        raise DuelNotYourTurnError("Сейчас не ваш ход!")

    aim_counts = state.get("aim_counts", {})
    actor_aim = aim_counts.get(str(actor_id), 0)

    if actor_aim < 10:
        actor_aim += 1
    aim_counts[str(actor_id)] = actor_aim

    opponent_id = duel.opponent_id if actor_id == duel.user_id else duel.user_id
    state["current_turn"] = opponent_id
    state["aim_counts"] = aim_counts
    duel.state = state
    await session.flush()

    return DuelActionResponse(
        session_id=duel.id,
        current_turn_user_id=opponent_id,
        opponent_user_id=actor_id,
        action="aim",
        is_finished=False,
        winner_id=None,
        aim_count=actor_aim,
        message=f"Игрок прицелился (уровень концентрации: {actor_aim}/10). Ход переходит к сопернику!",
    )


async def duel_shoot(
    session: AsyncSession,
    *,
    session_id: int,
    actor_id: int,
) -> DuelActionResponse:
    """Выстрел: расчет скрытого шанса (базовый 10% + 10% за прицеливание, максимум 95%)."""
    duel = await session.scalar(
        select(GameSession).where(GameSession.id == session_id).with_for_update()
    )
    if not duel or duel.status != "active":
        raise DuelError("Дуэль не активна")

    state = dict(duel.state or {})
    current_turn = state.get("current_turn")
    if actor_id != current_turn:
        raise DuelNotYourTurnError("Сейчас не ваш ход!")

    aim_counts = state.get("aim_counts", {})
    actor_aim = aim_counts.get(str(actor_id), 0)

    # Базовый шанс попадания увеличен до 25% (каждое прицеливание дает +10%, макс 95%)
    hit_chance = min(0.95, 0.25 + (actor_aim * 0.10))
    is_hit = random.random() <= hit_chance

    opponent_id = duel.opponent_id if actor_id == duel.user_id else duel.user_id

    if is_hit:
        # Попадание! Победа стрелка
        payout = duel.stake * 2
        duel.status = "finished"
        duel.payout = payout
        duel.result = f"winner:{actor_id}"
        duel.finished_at = datetime.now(timezone.utc)

        await credit_coins(
            session,
            user_id=actor_id,
            amount=payout,
            reason=f"Победа в дуэли #{duel.id}",
            operation_type="duel_win",
            reference=f"duel:{duel.id}",
        )
        await session.flush()

        return DuelActionResponse(
            session_id=duel.id,
            current_turn_user_id=actor_id,
            opponent_user_id=opponent_id,
            action="shot",
            is_finished=True,
            winner_id=actor_id,
            aim_count=actor_aim,
            message="💥 ВЫСТРЕЛ... ТОЧНОЕ ПОПАДАНИЕ! Победитель определен!",
        )
    else:
        # Промах! Прицеливание сбрасывается к нулю, ход переходит сопернику
        aim_counts[str(actor_id)] = 0
        state["aim_counts"] = aim_counts
        state["current_turn"] = opponent_id
        duel.state = state
        await session.flush()

        return DuelActionResponse(
            session_id=duel.id,
            current_turn_user_id=opponent_id,
            opponent_user_id=actor_id,
            action="shot",
            is_finished=False,
            winner_id=None,
            aim_count=0,
            message="💨 ВЫСТРЕЛ... ПРОМАХ! Прицеливание сброшено. Ход переходит к сопернику!",
        )
