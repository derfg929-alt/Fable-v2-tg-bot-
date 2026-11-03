"""Одиночные игры Fable: Рыбалка, Охота, Слоты, Рулетка.

Правила из ТЗ:
- Все игры используют глобальный баланс кристаллов/монет.
- Проигранные ставки сжигаются и удаляются из экономики.
- Выигрыши выплачиваются из средств, предусмотренных механикой игры.
- Лимиты ставок, награды и параметры игр настраивает владелец бота.
- Рыбалка и Охота имеют кулдауны, вероятность улова/добычи и начисление наград.
- Слоты и Рулетка проверяют лимиты, списывают ставку, генерируют исход, рассчитывают выигрыш или сжигают ставку.
- Сессии и результаты сохраняются в БД (GameSession, EconomyTransaction).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GameSession, GlobalSetting, Player
from app.services.economy_service import (
    burn_coins,
    credit_coins,
    InsufficientFundsError,
    InvalidAmountError,
    PlayerNotFoundError,
)


class GameError(Exception):
    """Базовая ошибка игр."""


class GameCooldownError(GameError):
    """Кулдаун на игру еще не прошел."""


@dataclass(frozen=True, slots=True)
class FishingResult:
    category: str
    item_name: str
    reward_coins: int
    message: str


@dataclass(frozen=True, slots=True)
class HuntingResult:
    category: str
    target_name: str
    reward_coins: int
    message: str


@dataclass(frozen=True, slots=True)
class SlotResult:
    reels: tuple[str, str, str]
    is_win: bool
    multiplier: float
    payout: int
    net_profit: int


@dataclass(frozen=True, slots=True)
class RouletteResult:
    winning_number: int
    winning_color: str
    is_win: bool
    multiplier: int
    payout: int
    net_profit: int


FISHING_LOOT = [
    ("empty", "Ничего не поймано", 0, 0.25),
    ("common", "Карась", 50, 0.45),
    ("rare", "Щука", 200, 0.20),
    ("epic", "Золотая рыбка", 1000, 0.08),
    ("legendary", "Древний сундук с сокровищами", 5000, 0.02),
]

HUNTING_LOOT = [
    ("failure", "Зверь убежал", 0, 0.30),
    ("common", "Заяц", 60, 0.40),
    ("rare", "Олень", 250, 0.20),
    ("epic", "Медведь", 1200, 0.08),
    ("legendary", "Мифический дракон", 6000, 0.02),
]

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣"]

ROULETTE_NUMBERS_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_NUMBERS_BLACK = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}


async def play_fishing(session: AsyncSession, *, user_id: int) -> FishingResult:
    """Рыбалка: проверка кулдауна, генерация улова, начисление награды."""
    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    # Проверка кулдауна (60 сек по умолчанию)
    cooldown_seconds = 60
    last_game = await session.scalar(
        select(GameSession)
        .where(
            GameSession.user_id == user_id,
            GameSession.game_type == "fishing",
        )
        .order_by(desc(GameSession.created_at))
        .limit(1)
    )
    now = datetime.now(timezone.utc)
    if last_game and last_game.created_at:
        elapsed = (now - last_game.created_at).total_seconds()
        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            raise GameCooldownError(f"Рыбачить можно через {remaining} сек.")

    # Выбор улова
    r = random.random()
    cumulative = 0.0
    category, item_name, reward, _ = FISHING_LOOT[0]
    for cat, name, rew, prob in FISHING_LOOT:
        cumulative += prob
        if r <= cumulative:
            category, item_name, reward, _ = cat, name, rew, prob
            break

    if reward > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=reward,
            reason=f"Улов на рыбалке: {item_name}",
            operation_type="game_fishing",
        )

    game_session = GameSession(
        game_type="fishing",
        status="finished",
        user_id=user_id,
        stake=0,
        payout=reward,
        result=f"{category}:{item_name}",
        state={"item_name": item_name, "category": category},
        finished_at=now,
    )
    session.add(game_session)
    await session.flush()

    msg = (
        f"🎣 Вы закинули удочку...\n"
        f"Улов: {item_name}!\n"
        + (f"Вы получили {reward} монет!" if reward > 0 else "К сожалению, ничего не клюнуло.")
    )
    return FishingResult(category=category, item_name=item_name, reward_coins=reward, message=msg)


async def play_hunting(session: AsyncSession, *, user_id: int) -> HuntingResult:
    """Охота: проверка кулдауна, генерация добычи, начисление награды."""
    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    cooldown_seconds = 60
    last_game = await session.scalar(
        select(GameSession)
        .where(
            GameSession.user_id == user_id,
            GameSession.game_type == "hunting",
        )
        .order_by(desc(GameSession.created_at))
        .limit(1)
    )
    now = datetime.now(timezone.utc)
    if last_game and last_game.created_at:
        elapsed = (now - last_game.created_at).total_seconds()
        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            raise GameCooldownError(f"На охоту можно отправиться через {remaining} сек.")

    r = random.random()
    cumulative = 0.0
    category, target_name, reward, _ = HUNTING_LOOT[0]
    for cat, name, rew, prob in HUNTING_LOOT:
        cumulative += prob
        if r <= cumulative:
            category, target_name, rew, prob = cat, name, rew, prob
            reward = rew
            break

    if reward > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=reward,
            reason=f"Добыча на охоте: {target_name}",
            operation_type="game_hunting",
        )

    game_session = GameSession(
        game_type="hunting",
        status="finished",
        user_id=user_id,
        stake=0,
        payout=reward,
        result=f"{category}:{target_name}",
        state={"target_name": target_name, "category": category},
        finished_at=now,
    )
    session.add(game_session)
    await session.flush()

    msg = (
        f"🏹 Вы отправились в чащу леса...\n"
        f"Добыча: {target_name}!\n"
        + (f"Награда: {reward} монет!" if reward > 0 else "Добыча ускользнула.")
    )
    return HuntingResult(category=category, target_name=target_name, reward_coins=reward, message=msg)


async def play_slots(session: AsyncSession, *, user_id: int, stake: int) -> SlotResult:
    """Слоты: списание ставки, вращение барабанов, расчет выигрыша, сжигание при проигрыше."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")

    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    if player.wallet < stake:
        raise InsufficientFundsError(f"Недостаточно монет: на счету {player.wallet}, ставка {stake}")

    # Списываем (сжигаем) ставку игрока
    await burn_coins(
        session,
        user_id=user_id,
        amount=stake,
        reason=f"Ставка в слотах: {stake} монет",
        operation_type="game_slots_stake",
    )

    r1, r2, r3 = random.choice(SLOT_SYMBOLS), random.choice(SLOT_SYMBOLS), random.choice(SLOT_SYMBOLS)
    reels = (r1, r2, r3)

    is_win = False
    multiplier = 0.0

    if r1 == r2 == r3:
        is_win = True
        if r1 == "7️⃣":
            multiplier = 20.0  # Джекпот
        elif r1 == "⭐":
            multiplier = 10.0
        elif r1 == "🔔":
            multiplier = 7.0
        else:
            multiplier = 5.0
    elif r1 == r2 or r2 == r3 or r1 == r3:
        # Две одинаковых
        is_win = True
        multiplier = 1.5

    payout = int(stake * multiplier)
    if payout > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=payout,
            reason=f"Выигрыш в слотах ({reels})",
            operation_type="game_slots_win",
        )

    now = datetime.now(timezone.utc)
    game_session = GameSession(
        game_type="slots",
        status="finished",
        user_id=user_id,
        stake=stake,
        payout=payout,
        result=f"{'WIN' if is_win else 'LOSE'}:{','.join(reels)}",
        state={"reels": list(reels), "multiplier": multiplier},
        finished_at=now,
    )
    session.add(game_session)
    await session.flush()

    return SlotResult(
        reels=reels,
        is_win=is_win,
        multiplier=multiplier,
        payout=payout,
        net_profit=payout - stake,
    )


async def play_roulette(
    session: AsyncSession,
    *,
    user_id: int,
    bet_target: str,  # 'red', 'black', or string number '0'..'36'
    stake: int,
) -> RouletteResult:
    """Рулетка: ставка на цвет (красное/черное 2x) или на число (36x)."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")

    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    if player.wallet < stake:
        raise InsufficientFundsError(f"Недостаточно монет: на счету {player.wallet}, ставка {stake}")

    # Валидация цели ставки
    target_clean = bet_target.strip().lower()
    if target_clean in ("красное", "red"):
        bet_type = "color"
        bet_val = "red"
    elif target_clean in ("черное", "чёрное", "black"):
        bet_type = "color"
        bet_val = "black"
    else:
        try:
            num = int(target_clean)
            if not (0 <= num <= 36):
                raise ValueError
            bet_type = "number"
            bet_val = num
        except ValueError:
            raise GameError("Ставка должна быть 'красное', 'черное' или числом от 0 до 36")

    # Списываем ставку
    await burn_coins(
        session,
        user_id=user_id,
        amount=stake,
        reason=f"Ставка в рулетке на {bet_target}: {stake} монет",
        operation_type="game_roulette_stake",
    )

    # Вращение рулетки: 0..36
    winning_number = random.randint(0, 36)
    if winning_number == 0:
        winning_color = "green"
    elif winning_number in ROULETTE_NUMBERS_RED:
        winning_color = "red"
    else:
        winning_color = "black"

    is_win = False
    multiplier = 0

    if bet_type == "color" and bet_val == winning_color:
        is_win = True
        multiplier = 2
    elif bet_type == "number" and bet_val == winning_number:
        is_win = True
        multiplier = 36

    payout = stake * multiplier
    if payout > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=payout,
            reason=f"Выигрыш в рулетке (выпало {winning_number} {winning_color})",
            operation_type="game_roulette_win",
        )

    now = datetime.now(timezone.utc)
    game_session = GameSession(
        game_type="roulette",
        status="finished",
        user_id=user_id,
        stake=stake,
        payout=payout,
        result=f"{'WIN' if is_win else 'LOSE'}:{winning_number}:{winning_color}",
        state={"winning_number": winning_number, "winning_color": winning_color, "bet": bet_target},
        finished_at=now,
    )
    session.add(game_session)
    await session.flush()

    return RouletteResult(
        winning_number=winning_number,
        winning_color=winning_color,
        is_win=is_win,
        multiplier=multiplier,
        payout=payout,
        net_profit=payout - stake,
    )
