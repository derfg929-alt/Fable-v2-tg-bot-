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
    debit_coins,
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

    # Базовая награда 50 монет + 10 за каждый день стрика (макс 150)
    coins = min(150, 50 + (streak - 1) * 10)
    exp = 5 * streak

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
    """Кости: бросок игрока против бота со сбалансированным RTP ~95% (выплата 2.0x, возврат при ничьей)."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")
    if stake > 50_000:
        raise InvalidAmountError("Максимальная ставка в костях: 50 000 монет")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or player.wallet < stake:
        raise InsufficientFundsError("Недостаточно монет для ставки")

    await burn_coins(session, user_id=user_id, amount=stake, reason="Ставка в костях", operation_type="game_dice_stake")

    p_roll = random.randint(1, 6)
    b_roll = random.randint(1, 6)

    # Легкое казино-преимущество (~4.3% house edge, RTP ~95.7%): в 8% победных бросков бот перебрасывает
    if p_roll > b_roll and random.random() < 0.08:
        b_roll = random.randint(p_roll, 6)

    is_win = p_roll > b_roll
    is_draw = p_roll == b_roll
    payout = 0

    if is_win:
        payout = int(stake * 2.0)
        await credit_coins(session, user_id=user_id, amount=payout, reason="Выигрыш в костях", operation_type="game_dice_win")
    elif is_draw:
        payout = stake
        await credit_coins(session, user_id=user_id, amount=payout, reason="Ничья в костях (возврат)", operation_type="game_dice_refund")

    return p_roll, b_roll, is_win, payout


async def play_coinflip(session: AsyncSession, *, user_id: int, choice: str, stake: int) -> tuple[str, bool, int]:
    """Монетка: орел или решка с честной удвоенной выплатой 2.0x и сбалансированным шансом победы (48%, RTP 96%)."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")
    if stake > 50_000:
        raise InvalidAmountError("Максимальная ставка в монетке: 50 000 монет")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or player.wallet < stake:
        raise InsufficientFundsError("Недостаточно монет для ставки")

    await burn_coins(session, user_id=user_id, amount=stake, reason="Ставка в монетке", operation_type="game_coinflip_stake")

    norm_choice = choice.lower()
    is_user_heads = norm_choice in ("орел", "орёл", "heads")

    # Сбалансированный шанс на лудку: 48% на победу игрока (RTP = 96%)
    if random.random() < 0.48:
        outcome = "орел" if is_user_heads else "решка"
    else:
        outcome = "решка" if is_user_heads else "орел"

    is_win = (is_user_heads and outcome == "орел") or \
             (norm_choice in ("решка", "tails") and outcome == "решка")

    payout = int(stake * 2.0) if is_win else 0
    if is_win:
        await credit_coins(session, user_id=user_id, amount=payout, reason="Выигрыш в монетке", operation_type="game_coinflip_win")

    return outcome, is_win, payout


async def play_mystery_box(session: AsyncSession, *, user_id: int, stake: int) -> tuple[float, int]:
    """Таинственный сундук: частые награды (73% шанс выпадения монет) со сбалансированным RTP ~95%."""
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")
    if stake > 50_000:
        raise InvalidAmountError("Максимальная ставка в сундуке: 50 000 монет")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or player.wallet < stake:
        raise InsufficientFundsError("Недостаточно монет для ставки")

    await burn_coins(session, user_id=user_id, amount=stake, reason="Открытие сундука", operation_type="game_box_stake")

    # Сбалансированные веса: 73% шанс непустого сундука, RTP ~95.0%
    mult = random.choices([0.0, 0.5, 1.2, 1.5, 2.0, 3.0, 5.0], weights=[0.27, 0.19, 0.25, 0.17, 0.08, 0.03, 0.01])[0]
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


from app.db.models import PlayerInventory
from app.services.gear_service import get_or_create_gear

HUNTING_PRICES: dict[str, int] = {
    "заяц": 75,
    "олень": 320,
    "медведь": 1_500,
    "дракон": 7_500,
}

FISHING_PRICES: dict[str, int] = {
    "карась": 60,
    "щука": 250,
    "золотая рыбка": 1_200,
    "сундук с сокровищами": 6_000,
}

FISHING_LOOT = [
    ("empty", "Ничего не поймано", 0, 0.30, None),
    ("common", "Карась", 60, 0.55, "карась"),
    ("rare", "Щука", 250, 0.12, "щука"),
    ("epic", "Золотая рыбка", 1200, 0.025, "золотая рыбка"),
    ("legendary", "Древний сундук с сокровищами", 6000, 0.005, "сундук с сокровищами"),
]

HUNTING_LOOT = [
    ("failure", "Зверь убежал", 0, 0.35, None),
    ("common", "Заяц", 75, 0.50, "заяц"),
    ("rare", "Олень", 320, 0.12, "олень"),
    ("epic", "Медведь", 1500, 0.025, "медведь"),
    ("legendary", "Мифический дракон", 7500, 0.005, "дракон"),
]

# Реальные символы Telegram-слотов (4 символа, закодированные по 2 бита)
# 0: BAR, 1: 🍇, 2: 🍋, 3: 7️⃣
TELEGRAM_SLOT_SYMBOLS = ["BAR", "🍇", "🍋", "7️⃣"]
SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣"]

def decode_telegram_slot_dice(dice_value: int) -> tuple[str, str, str]:
    """Декодирует значение Telegram слотов (1..64) в тройку барабанов."""
    val = max(1, min(64, dice_value)) - 1
    r1 = TELEGRAM_SLOT_SYMBOLS[val & 3]
    r2 = TELEGRAM_SLOT_SYMBOLS[(val >> 2) & 3]
    r3 = TELEGRAM_SLOT_SYMBOLS[(val >> 4) & 3]
    return (r1, r2, r3)

ROULETTE_NUMBERS_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
ROULETTE_NUMBERS_BLACK = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}


async def play_fishing(session: AsyncSession, *, user_id: int) -> FishingResult:
    """Рыбалка: проверка кулдауна, влияние удочки из снаряжения, сохранение улова в инвентарь."""
    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    cooldown_seconds = 600  # 10 минут
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

    gear = await get_or_create_gear(session, user_id=user_id)
    rod_lvl = gear.fishing_rod_level

    # Шанс холостой поклевки уменьшается с уровнем удочки (при 10 уровне удочки исключен)
    empty_prob = max(0.0, 0.20 - (rod_lvl - 1) * 0.025)
    r = random.random()

    if r < empty_prob:
        category, item_name, reward_coins, item_key = "empty", "Ничего не поймано", 0, None
    else:
        # Уменьшенные шансы на дорогой дроп (золотая рыбка и сундук)
        weights = [
            0.70,
            0.22 + (rod_lvl - 1) * 0.01,
            0.06 + (rod_lvl - 1) * 0.005,
            0.01 + (rod_lvl - 1) * 0.002,
        ]
        chosen = random.choices(FISHING_LOOT[1:], weights=weights)[0]
        category, item_name, reward_coins, _, item_key = chosen

    if item_key:
        inv = await session.scalar(
            select(PlayerInventory).where(
                PlayerInventory.user_id == user_id,
                PlayerInventory.item_id == item_key,
            ).with_for_update()
        )
        if not inv:
            inv = PlayerInventory(
                user_id=user_id,
                item_id=item_key,
                item_name=item_name,
                category="fishing",
                quantity=1,
                is_exclusive=False,
            )
            session.add(inv)
        else:
            inv.quantity += 1

        player.experience += 10 + rod_lvl * 2

    game_session = GameSession(
        game_type="fishing",
        status="finished",
        user_id=user_id,
        stake=0,
        payout=reward_coins,
        result=f"{category}:{item_name}",
        state={"item_name": item_name, "category": category, "rod_level": rod_lvl},
        finished_at=now,
    )
    session.add(game_session)
    await session.flush()

    if item_key:
        msg = (
            f"🎣 <b>РЫБАЛКА УДАЛАСЬ!</b> (Удочка ур. {rod_lvl})\n\n"
            f"Вы выудили: <b>{item_name}</b> (1 шт.)!\n"
            f"📦 <i>Улов добавлен в ваш <code>инвентарь</code>. Продайте его скупщику через <code>инвентарь</code>!</i>\n"
            f"Оценочная стоимость: <b>~{reward_coins}</b> монет."
        )
    else:
        msg = f"🎣 <b>РЫБАЛКА</b> (Удочка ур. {rod_lvl})\n\nПоплавок дернулся, но рыба сорвалась с крючка! Попробуйте позже или улучшите удочку: <code>улучшить удочка</code>."

    return FishingResult(category=category, item_name=item_name, reward_coins=reward_coins, message=msg)


async def play_hunting(session: AsyncSession, *, user_id: int) -> HuntingResult:
    """Охота: проверка кулдауна, влияние охотничьего оружия, добыча в инвентарь."""
    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    cooldown_seconds = 600  # 10 минут
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

    gear = await get_or_create_gear(session, user_id=user_id)
    rifle_lvl = gear.hunting_rifle_level

    # Промах снижается с прокачкой оружия (при 10 уровне оружия промах исключен)
    miss_prob = max(0.0, 0.20 - (rifle_lvl - 1) * 0.025)
    r = random.random()

    if r < miss_prob:
        category, target_name, reward_coins, item_key = "failure", "Зверь убежал", 0, None
    else:
        # Уменьшенные шансы на дорогой трофей (медведь и дракон)
        weights = [
            0.70,
            0.22 + (rifle_lvl - 1) * 0.01,
            0.06 + (rifle_lvl - 1) * 0.005,
            0.01 + (rifle_lvl - 1) * 0.002,
        ]
        chosen = random.choices(HUNTING_LOOT[1:], weights=weights)[0]
        category, target_name, reward_coins, _, item_key = chosen

    if item_key:
        inv = await session.scalar(
            select(PlayerInventory).where(
                PlayerInventory.user_id == user_id,
                PlayerInventory.item_id == item_key,
            ).with_for_update()
        )
        if not inv:
            inv = PlayerInventory(
                user_id=user_id,
                item_id=item_key,
                item_name=target_name,
                category="hunting",
                quantity=1,
                is_exclusive=False,
            )
            session.add(inv)
        else:
            inv.quantity += 1

        player.experience += 15 + rifle_lvl * 3

    game_session = GameSession(
        game_type="hunting",
        status="finished",
        user_id=user_id,
        stake=0,
        payout=reward_coins,
        result=f"{category}:{target_name}",
        state={"target_name": target_name, "category": category, "rifle_level": rifle_lvl},
        finished_at=now,
    )
    session.add(game_session)
    await session.flush()

    if item_key:
        msg = (
            f"🏹 <b>УДАЧНАЯ ОХОТА!</b> (Оружие ур. {rifle_lvl})\n\n"
            f"Вы метко подстрелили добычу: <b>{target_name}</b> (1 шт.)!\n"
            f"📦 <i>Трофей сохранен в ваш <code>инвентарь</code>. Чтобы получить деньги, продайте его в магазин: <code>инвентарь</code>!</i>\n"
            f"Оценочная стоимость: <b>~{reward_coins}</b> монет."
        )
    else:
        msg = f"🏹 <b>ОХОТА</b> (Оружие ур. {rifle_lvl})\n\nЗверь скрылся в густой чаще леса! Улучшите оружие для охоты: <code>улучшить оружие</code>."

    return HuntingResult(category=category, target_name=target_name, reward_coins=reward_coins, message=msg)


async def play_slots(session: AsyncSession, *, user_id: int, stake: int, dice_value: int | None = None) -> SlotResult:
    """Слоты: списание ставки, вращение барабанов, сбалансированные шансы как в классических казино.
    RTP ~94-96%, защита от бесконечного выигрыша миллионов и предотвращение постоянных сливов.
    """
    if stake <= 0:
        raise InvalidAmountError("Ставка должна быть больше нуля")
    # Ограничение максимальной ставки в слотах (максимум 50 000 за спин, чтобы избежать выигрыша миллионов)
    if stake > 50_000:
        raise InvalidAmountError("Максимальная ставка в слотах: 50 000 монет")

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

    if dice_value is not None:
        reels = decode_telegram_slot_dice(dice_value)
    else:
        # Реалистичные шансы:
        # 1.5% джекпот 777 (как в реальных барабанах 1/64)
        # 8.5% три одинаковых символа (x5)
        # 35.0% пара одинаковых символов (частичный возврат/небольшой выигрыш x0.8 - x1.2)
        # 55.0% разные символы (проигрыш)
        # Итоговая частота побед/возвратов: ~45%, RTP ~94.7%
        spin_type = random.choices(["jackpot", "three", "pair", "diff"], weights=[0.015, 0.085, 0.35, 0.55])[0]
        symbols_pool = ["🍇", "🍋", "BAR", "🍒", "🔔"]
        if spin_type == "jackpot":
            reels = ("7️⃣", "7️⃣", "7️⃣")
        elif spin_type == "three":
            sym = random.choice(symbols_pool)
            reels = (sym, sym, sym)
        elif spin_type == "pair":
            sym = random.choice(["7️⃣"] + symbols_pool)
            other = random.choice([s for s in (["7️⃣"] + symbols_pool) if s != sym])
            slots_list = [sym, sym, other]
            random.shuffle(slots_list)
            reels = (slots_list[0], slots_list[1], slots_list[2])
        else:
            shuffled = random.sample(["7️⃣"] + symbols_pool, 3)
            reels = (shuffled[0], shuffled[1], shuffled[2])

    r1, r2, r3 = reels
    is_win = False
    multiplier = 0.0

    if r1 == r2 == r3:
        is_win = True
        if r1 == "7️⃣":
            multiplier = 15.0  # Джекпот 777 (шанс 1/64 = ~1.56%)
        elif r1 == "🍇":
            multiplier = 5.0   # Три винограда (1/64)
        elif r1 == "🍋":
            multiplier = 5.0   # Три лимона (1/64)
        elif r1 == "BAR":
            multiplier = 5.0   # Три BAR (1/64)
        else:
            multiplier = 5.0
    elif r1 == r2 or r2 == r3 or r1 == r3:
        # Пара одинаковых символов
        is_win = True
        if (r1 == "7️⃣" and r2 == "7️⃣") or (r2 == "7️⃣" and r3 == "7️⃣") or (r1 == "7️⃣" and r3 == "7️⃣"):
            multiplier = 1.2
        else:
            multiplier = 0.8

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


async def play_crash_game(
    session: AsyncSession,
    *,
    user_id: int,
    stake: int,
) -> tuple[float, bool, int]:
    """Игра 'Краш': динамичный множитель со сбалансированным RTP ~94.6%."""
    if stake <= 0:
        raise GameError("Ставка должна быть больше нуля")
    if stake > 50_000:
        raise GameError("Максимальная ставка: 50 000 монет")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")
    if player.wallet < stake:
        raise InsufficientFundsError(f"Недостаточно монет: ставка {stake:,}, баланс {player.wallet:,}".replace(",", " "))

    await debit_coins(session, user_id=user_id, amount=stake, reason="Ставка в игре Краш", operation_type="game_bet")

    # Сбалансированный шанс: 44% шанс забрать от x1.40 до x2.90 (среднее 2.15, RTP ~94.6%)
    r = random.random()
    if r < 0.44:
        mult = round(random.uniform(1.4, 2.9), 2)
        is_win = True
        payout = int(stake * mult)
        await credit_coins(session, user_id=user_id, amount=payout, reason=f"Выигрыш в Краш (x{mult})", operation_type="game_win")
    else:
        mult = round(random.uniform(1.0, 1.35), 2)
        is_win = False
        payout = 0

    return mult, is_win, payout


async def play_shooting_range_game(
    session: AsyncSession,
    *,
    user_id: int,
    stake: int,
) -> tuple[str, float, int]:
    """Мини-игра 'Тир': стрельба по мишеням с 54% шансом попадания и RTP ~95.8%."""
    if stake <= 0:
        raise GameError("Ставка должна быть больше нуля")
    if stake > 50_000:
        raise GameError("Максимальная ставка: 50 000 монет")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")
    if player.wallet < stake:
        raise InsufficientFundsError(f"Недостаточно монет: ставка {stake:,}, баланс {player.wallet:,}".replace(",", " "))

    await debit_coins(session, user_id=user_id, amount=stake, reason="Выстрел в Тире", operation_type="game_bet")

    # Сбалансированные цели и коэффициенты (RTP ~95.8%):
    targets = [
        ("🎯 В «яблочко» (Центр)", 2.5, 0.16),
        ("🦆 Движущаяся утка", 1.8, 0.20),
        ("🥫 Жестяная банка", 1.1, 0.18),
        ("💨 Промах по мишени", 0.0, 0.46),
    ]

    r = random.random()
    accum = 0.0
    chosen_target = targets[-1]
    for target_name, mult, prob in targets:
        accum += prob
        if r <= accum:
            chosen_target = (target_name, mult, prob)
            break

    target_name, mult, _ = chosen_target
    payout = int(stake * mult)
    if payout > 0:
        await credit_coins(session, user_id=user_id, amount=payout, reason=f"Попадание в Тире: {target_name}", operation_type="game_win")

    return target_name, mult, payout


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
