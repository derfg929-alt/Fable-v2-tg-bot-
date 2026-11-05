"""Сервис шахты и переработки руды для Fable.

Правила из ТЗ:
- Шахта: есть уровни кирки/шахты.
- На 1 уровне выпадает только уголь (coal).
- На 2+ уровнях выпадают цветные и благородные металлы: железо, медь, серебро, золото.
- Способы обработки: переплавка в слитки (`processed_metals`).
- Сбалансированный доход: обработка не дает слишком много денег, но выгоднее продажи сырой руды.
- Кулдаун копания: 5 минут.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerMine
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError


class MineError(Exception):
    """Базовая ошибка шахты."""


class MineCooldownError(MineError):
    """Кулдаун на копку в шахте."""


MINE_COOLDOWN_SECONDS = 300  # 5 минут
MAX_PICKAXE_LEVEL = 10

# Базовая стоимость продажи сырой руды в монетах за 1 единицу
ORE_PRICES: dict[str, int] = {
    "coal": 2,             # Уголь
    "iron": 6,             # Железо
    "copper": 10,          # Медь
    "silver": 25,          # Серебро
    "gold": 60,            # Золото
    "processed_metals": 120, # Слиток обработанного металла
}


def get_pickaxe_upgrade_cost(level: int) -> int:
    return level * 3_000


async def get_or_create_mine(session: AsyncSession, *, user_id: int) -> PlayerMine:
    mine = await session.scalar(
        select(PlayerMine).where(PlayerMine.user_id == user_id).with_for_update()
    )
    if not mine:
        mine = PlayerMine(user_id=user_id, pickaxe_level=1)
        session.add(mine)
        await session.flush()
    return mine


@dataclass(frozen=True, slots=True)
class MineDigResult:
    pickaxe_level: int
    ores_found: dict[str, int]
    exp_gained: int
    message: str


async def dig_in_mine(session: AsyncSession, *, user_id: int) -> MineDigResult:
    """Копание в шахте. Возвращает найденные ресурсы."""
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    mine = await get_or_create_mine(session, user_id=user_id)
    now = datetime.now(timezone.utc)

    if mine.last_mined_at:
        elapsed = (now - mine.last_mined_at).total_seconds()
        if elapsed < MINE_COOLDOWN_SECONDS:
            rem = int(MINE_COOLDOWN_SECONDS - elapsed)
            mins = rem // 60
            secs = rem % 60
            raise MineCooldownError(f"Кирка остывает! Следующий спуск в шахту через {mins} мин. {secs} сек.")

    found: dict[str, int] = {}
    lvl = mine.pickaxe_level

    # На 1 уровне — ТОЛЬКО уголь
    if lvl == 1:
        coal_cnt = random.randint(5, 15)
        mine.coal += coal_cnt
        found["coal"] = coal_cnt
        exp = 5
    else:
        # На 2+ уровнях — уголь и цветные металлы
        coal_cnt = random.randint(10, 20 + lvl * 2)
        mine.coal += coal_cnt
        found["coal"] = coal_cnt

        iron_cnt = random.randint(3, 8 + lvl)
        mine.iron += iron_cnt
        found["iron"] = iron_cnt

        if lvl >= 3:
            copper_cnt = random.randint(2, 6 + lvl)
            mine.copper += copper_cnt
            found["copper"] = copper_cnt

        if lvl >= 5:
            silver_cnt = random.randint(1, 4 + lvl // 2)
            mine.silver += silver_cnt
            found["silver"] = silver_cnt

        if lvl >= 7:
            gold_cnt = random.randint(1, 2 + lvl // 3)
            mine.gold += gold_cnt
            found["gold"] = gold_cnt

        exp = 10 + lvl * 3

    player.experience += exp
    mine.last_mined_at = now
    await session.flush()

    lines = [f"⛏ <b>СПУСК В ШАХТУ (Кирка ур. {lvl})</b>\n\nВы успешно добыли руду:"]
    for ore_name, count in found.items():
        title = {
            "coal": "🪨 Уголь",
            "iron": "🔩 Железная руда",
            "copper": "🥉 Медная руда",
            "silver": "🥈 Серебряная руда",
            "gold": "🥇 Золотая руда",
        }.get(ore_name, ore_name)
        lines.append(f"• {title}: <b>+{count} шт.</b>")

    lines.append(f"\n✨ Получено опыта: <b>+{exp}</b> XP")
    lines.append("💡 <i>Используйте <code>шахта переплавка</code> для обработки или продайте в <code>инвентарь</code>!</i>")

    return MineDigResult(
        pickaxe_level=lvl,
        ores_found=found,
        exp_gained=exp,
        message="\n".join(lines),
    )


async def upgrade_pickaxe(session: AsyncSession, *, user_id: int) -> tuple[int, int]:
    """Улучшает кирку в шахте."""
    mine = await get_or_create_mine(session, user_id=user_id)
    if mine.pickaxe_level >= MAX_PICKAXE_LEVEL:
        raise MineError("Кирка уже максимального 10 уровня!")

    cost = get_pickaxe_upgrade_cost(mine.pickaxe_level)
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or player.wallet < cost:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cost:,} монет, у вас {player.wallet if player else 0:,}".replace(",", " "))

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Улучшение кирки до ур. {mine.pickaxe_level + 1}",
        operation_type="mine_upgrade",
    )

    mine.pickaxe_level += 1
    await session.flush()
    return mine.pickaxe_level, cost


async def smelt_ores(session: AsyncSession, *, user_id: int) -> tuple[int, str]:
    """Переплавляет руду в обработанные металлические слитки.

    Рецепт: 10 угля + 5 железа + 2 меди = 1 обработанный слиток.
    Если меди нет, но есть 20 угля + 10 железа = 1 слиток.
    """
    mine = await get_or_create_mine(session, user_id=user_id)
    if mine.coal < 10 or mine.iron < 5:
        raise MineError(
            "Для переплавки требуется как минимум 10 угля и 5 железа!\n"
            f"У вас: {mine.coal} угля, {mine.iron} железа."
        )

    # Максимальное количество слитков за раз
    possible_from_copper = (mine.copper // 2) if mine.copper >= 2 else 0
    possible = min(mine.coal // 10, mine.iron // 5)
    if possible_from_copper > 0:
        bars = min(possible, possible_from_copper)
        used_coal = bars * 10
        used_iron = bars * 5
        used_copper = bars * 2
    else:
        bars = min(mine.coal // 20, mine.iron // 10)
        if bars <= 0:
            raise MineError("Недостаточно сырья для плавки (нужно 20 угля и 10 железа на слиток).")
        used_coal = bars * 20
        used_iron = bars * 10
        used_copper = 0

    mine.coal -= used_coal
    mine.iron -= used_iron
    mine.copper -= used_copper
    mine.processed_metals += bars

    await session.flush()

    msg = (
        f"🔥 <b>ПЕРЕПЛАВКА РУДЫ В КУЗНИЦЕ</b>\n\n"
        f"Успешно переплавлено:\n"
        f"• Потрачено угля: <b>{used_coal}</b> шт.\n"
        f"• Потрачено железа: <b>{used_iron}</b> шт.\n"
        + (f"• Потрачено меди: <b>{used_copper}</b> шт.\n" if used_copper else "")
        + f"\n✨ Получено: <b>+{bars}</b> слитков обработанного металла! 💎\n"
        f"Всего слитков: <b>{mine.processed_metals}</b> шт.\n\n"
        f"Слитки можно выгодно продать через <code>инвентарь</code>!"
    )
    return bars, msg
