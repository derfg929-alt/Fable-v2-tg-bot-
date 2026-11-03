"""Сервис майнинг-фермы крипты для Fable.

Механика:
- Игрок строит ферму: покупает майнинг-компьютеры и улучшает уровень фермы (охлаждение, видеокарты, ASIC-чипы).
- Компьютеры майнят криптовалюту и монеты.
- Стоимость 1 компьютера: 2 500 монет.
- Улучшение уровня фермы: уровень * 10 000 монет.
- Доходность: каждый компьютер приносит 15 монет/час * множитель уровня фермы (1x на 1 уровне, +25% за уровень).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerMining
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError


class MiningError(Exception):
    """Базовая ошибка майнинга."""


COMPUTER_PRICE = 2_500
MAX_FARM_LEVEL = 10


def get_farm_upgrade_cost(current_level: int) -> int:
    return current_level * 10_000


def get_hourly_yield(computers: int, farm_level: int) -> int:
    multiplier = 1.0 + (farm_level - 1) * 0.25
    return int(computers * 25 * multiplier)


async def get_or_create_mining(session: AsyncSession, *, user_id: int) -> PlayerMining:
    row = await session.scalar(
        select(PlayerMining).where(PlayerMining.user_id == user_id).with_for_update()
    )
    if not row:
        row = PlayerMining(
            user_id=user_id,
            computers=1,  # 1 компьютер при открытии фермы
            farm_level=1,
            last_mined_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
    return row


async def buy_computers(session: AsyncSession, *, user_id: int, count: int = 1) -> tuple[PlayerMining, int]:
    if count <= 0:
        raise MiningError("Количество компьютеров должно быть больше нуля")

    cost = count * COMPUTER_PRICE
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cost:,} монет, у вас {player.wallet:,}".replace(",", " "))

    mining = await get_or_create_mining(session, user_id=user_id)

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Покупка {count} майнинг-компьютеров",
        operation_type="mining_buy_computer",
    )

    mining.computers += count
    await session.flush()
    return mining, cost


async def upgrade_mining_farm(session: AsyncSession, *, user_id: int) -> tuple[PlayerMining, int]:
    mining = await get_or_create_mining(session, user_id=user_id)
    if mining.farm_level >= MAX_FARM_LEVEL:
        raise MiningError("Ферма уже достигла максимального 10 уровня!")

    cost = get_farm_upgrade_cost(mining.farm_level)
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(f"Недостаточно монет для улучшения: требуется {cost:,} монет, у вас {player.wallet:,}".replace(",", " "))

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Улучшение фермы до уровня {mining.farm_level + 1}",
        operation_type="mining_upgrade_farm",
    )

    mining.farm_level += 1
    await session.flush()
    return mining, cost


async def collect_mining_profit(session: AsyncSession, *, user_id: int) -> tuple[int, int]:
    """Собирает намайненные монеты. Возвращает (намайнено, компьютеров)."""
    mining = await get_or_create_mining(session, user_id=user_id)
    now = datetime.now(timezone.utc)
    last = mining.last_mined_at or now

    elapsed_hours = (now - last).total_seconds() / 3600.0
    if elapsed_hours < (1.0 / 60.0):
        return 0, mining.computers

    yield_per_hour = get_hourly_yield(mining.computers, mining.farm_level)
    coins = int(yield_per_hour * elapsed_hours)

    if coins > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=coins,
            reason="Сбор дохода с крипто-фермы",
            operation_type="mining_profit",
        )
        mining.last_mined_at = now
        await session.flush()

    return coins, mining.computers
