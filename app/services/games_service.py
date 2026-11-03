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

from app.db.models import DailyBonus, GameSession, GlobalSetting, Player
from app.services.economy_service import (
    burn_coins,
    credit_coins,
    InsufficientFundsError,
    InvalidAmountError,
    PlayerNotFoundError,
)


@dataclass(frozen=True, slots=True)
class DailyBonusResult:
    reward_coins: int
    reward_exp: int
    streak: int
    message: str


async def claim_daily_bonus(session: AsyncSession, *, user_id: int) -> DailyBonusResult:
    """Ежедневный бонус раз в 24 часа со стриками."""
    now = datetime.now(timezone.utc)
    bonus_row = await session.scalar(
        select(DailyBonus).where(DailyBonus.user_id == user_id).with_for_update()
    )
    streak = 1
    if bonus_row:
        elapsed = (now - bonus_row.last_claimed_at).total_seconds()
        if elapsed < 86400:  # 24 часа
            remaining = int(86400 - elapsed)
            hours = remaining // 3600
            mins = (remaining % 3600) // 60
            raise GameCooldownError(f"Вы уже забрали ежедневный бонус! Следующий доступен через {hours} ч. {mins} мин.")
        elif elapsed <= 172800:  # 48 часов - сохраняем стрик
            streak = bonus_row.streak + 1
        else:
            streak = 1
        bonus_row.streak = streak
        bonus_row.last_claimed_at = now
    else:
        bonus_row = DailyBonus(user_id=user_id, streak=1, last_claimed_at=now)
        session.add(bonus_row)

    # Базовая награда 500 монет + 100 за каждый день стрика (макс 2 500)
    coins = min(2500, 500 + (streak - 1) * 100)
    exp = 25 * streak

    await credit_coins(
        session,
        user_id=user_id,
        amount=coins,
        reason=f"Ежедневный бонус (день {streak})",
        operation_type="daily_bonus",
    )

    player = await session.get(Player, user_id)
    if player:
        player.experience += exp

    msg = (
        f"🎁 <b>ЕЖЕДНЕВНЫЙ БОНУС ПОЛУЧЕН!</b>\n\n"
        f"🔥 Серия входов: <b>{streak} день подряд!</b>\n"
        f"💰 Награда: <b>+{coins:,}</b> монет\n"
        f"✨ Опыт: <b>+{exp}</b> XP\n\n"
        f"<i>Возвращайтесь завтра, чтобы увеличить награду!</i>"
    ).replace(",", " ")

    await session.flush()
    return DailyBonusResult(reward_coins=coins, reward_exp=exp, streak=streak, message=msg)


# ===================== МИНИ-ИГРЫ =====================

async def play_dice(session: AsyncSession, *, user_id: int, stake: int) -> tuple[int, int, bool, int]:
    """Кости: бросок игрока vs бота. Возвращает (player_roll, bot_roll, is_win, payout)."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or player.wallet < stake:
        raise InsufficientFundsError("Недостаточно монет для ставки")

    await burn_coins(session, user_id=user_id, amount=stake, reason="Ставка в костях", operation_type="game_dice_stake")

    p_roll = random.randint(1, 6)
    b_roll = random.randint(1, 6)

    is_win = p_roll > b_roll
    is_draw = p_roll == b_roll
    payout = 0

    if is_win:
        payout = int(stake * 2)
        await credit_coins(session, user_id=user_id, amount=payout, reason="Выигрыш в костях", operation_type="game_dice_win")
    elif is_draw:
        payout = stake
        await credit_coins(session, user_id=user_id, amount=payout, reason="Ничья в костях (возврат)", operation_type="game_dice_refund")

    return p_roll, b_roll, is_win, payout


async def play_coinflip(session: AsyncSession, *, user_id: int, choice: str, stake: int) -> tuple[str, bool, int]:
    """Монетка: орел или решка. Возвращает (outcome, is_win, payout)."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or player.wallet < stake:
        raise InsufficientFundsError("Недостаточно монет для ставки")

    await burn_coins(session, user_id=user_id, amount=stake, reason="Ставка в монетке", operation_type="game_coinflip_stake")

    outcome = random.choice(["орел", "решка"])
    is_win = (choice.lower() in ("орел", "орёл", "heads") and outcome == "орел") or \
             (choice.lower() in ("решка", "tails") and outcome == "решка")

    payout = int(stake * 1.95) if is_win else 0
    if is_win:
        await credit_coins(session, user_id=user_id, amount=payout, reason="Выигрыш в монетке", operation_type="game_coinflip_win")

    return outcome, is_win, payout


async def play_mystery_box(session: AsyncSession, *, user_id: int, stake: int) -> tuple[float, int]:
    """Таинственный сундук: коэффициент от 0x до 5x. Возвращает (multiplier, payout)."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or player.wallet < stake:
        raise InsufficientFundsError("Недостаточно монет для ставки")

    await burn_coins(session, user_id=user_id, amount=stake, reason="Открытие сундука", operation_type="game_box_stake")

    mult = random.choices([0.0, 0.5, 1.2, 1.8, 2.5, 5.0], weights=[0.30, 0.25, 0.20, 0.15, 0.08, 0.02])[0]
    payout = int(stake * mult)
    if payout > 0:
        await credit_coins(session, user_id=user_id, amount=payout, reason=f"Выигрыш в сундуке (x{mult})", operation_type="game_box_win")

    return mult, payout


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

    # Проверка кулдауна (1 час = 3600 сек)
    cooldown_seconds = 3600
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
            rem = int(cooldown_seconds - elapsed)
            mins = rem // 60
            secs = rem % 60
            raise GameCooldownError(f"Рыбачить можно через {mins} мин. {secs} сек.")

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

    cooldown_seconds = 3600
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
            rem = int(cooldown_seconds - elapsed)
            mins = rem // 60
            secs = rem % 60
            raise GameCooldownError(f"На охоту можно отправиться через {mins} мин. {secs} сек.")

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
