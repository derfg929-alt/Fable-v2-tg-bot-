"""Сервис бизнесов для Fable (10 бизнесов с последующим улучшением).

Список 10 бизнесов:
1. Ларёк с шаурмой (цена: 1 000, базовая прибыль: 100/час)
2. Кофейня (цена: 5 000, базовая прибыль: 400/час)
3. Автомойка (цена: 20 000, базовая прибыль: 1 500/час)
4. Барбершоп (цена: 50 000, базовая прибыль: 3 500/час)
5. Ресторан (цена: 150 000, базовая прибыль: 10 000/час)
6. Ночной клуб (цена: 500 000, базовая прибыль: 30 000/час)
7. Сеть супермаркетов (цена: 1 500 000, базовая прибыль: 80 000/час)
8. Строительная компания (цена: 5 000 000, базовая прибыль: 250 000/час)
9. IT-Корпорация (цена: 15 000 000, базовая прибыль: 700 000/час)
10. Нефтяной холдинг (цена: 50 000 000, базовая прибыль: 2 200 000/час)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerBusiness
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError


class BusinessError(Exception):
    """Базовая ошибка бизнесов."""


@dataclass(frozen=True, slots=True)
class BusinessConfig:
    id: int
    name: str
    icon: str
    base_cost: int
    base_income_hourly: int


# Окупаемость ровно 7 дней (168 часов): доход в час = base_cost / 168
BUSINESSES: dict[int, BusinessConfig] = {
    1: BusinessConfig(1, "Ларёк с шаурмой", "🌯", 1_000, 6),
    2: BusinessConfig(2, "Кофейня", "☕️", 5_000, 30),
    3: BusinessConfig(3, "Автомойка", "🚗", 20_000, 120),
    4: BusinessConfig(4, "Барбершоп", "💈", 50_000, 300),
    5: BusinessConfig(5, "Ресторан", "🍽", 150_000, 900),
    6: BusinessConfig(6, "Ночной клуб", "🍸", 500_000, 3_000),
    7: BusinessConfig(7, "Сеть супермаркетов", "🏬", 1_500_000, 9_000),
    8: BusinessConfig(8, "Строительная компания", "🏗", 5_000_000, 30_000),
    9: BusinessConfig(9, "IT-Корпорация", "💻", 15_000_000, 90_000),
    10: BusinessConfig(10, "Нефтяной холдинг", "🛢", 50_000_000, 300_000),
}


def get_upgrade_cost(b_id: int, current_level: int) -> int:
    cfg = BUSINESSES[b_id]
    return int(cfg.base_cost * (current_level + 1))


def get_hourly_income(b_id: int, current_level: int) -> int:
    cfg = BUSINESSES[b_id]
    # Каждый уровень дает +20% к базовой прибыли
    return int(cfg.base_income_hourly * (1 + (current_level - 1) * 0.2))


async def get_player_businesses(session: AsyncSession, *, user_id: int) -> list[PlayerBusiness]:
    res = await session.scalars(
        select(PlayerBusiness).where(PlayerBusiness.user_id == user_id).order_by(PlayerBusiness.business_id)
    )
    return list(res.all())


async def buy_business(session: AsyncSession, *, user_id: int, business_id: int) -> tuple[PlayerBusiness, BusinessConfig]:
    if business_id not in BUSINESSES:
        raise BusinessError(f"Бизнес с номером {business_id} не существует (выберите 1..10)")

    cfg = BUSINESSES[business_id]
    existing = await session.scalar(
        select(PlayerBusiness).where(
            PlayerBusiness.user_id == user_id,
            PlayerBusiness.business_id == business_id,
        )
    )
    if existing:
        raise BusinessError(f"У вас уже куплен бизнес «{cfg.name}»! Его можно улучшить.")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cfg.base_cost:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cfg.base_cost}, у вас {player.wallet}")

    await debit_coins(
        session,
        user_id=user_id,
        amount=cfg.base_cost,
        reason=f"Покупка бизнеса #{business_id} ({cfg.name})",
        operation_type="business_buy",
        reference=f"business:{business_id}",
    )

    pb = PlayerBusiness(
        user_id=user_id,
        business_id=business_id,
        level=1,
        last_collected_at=datetime.now(timezone.utc),
    )
    session.add(pb)
    await session.flush()
    return pb, cfg


async def upgrade_business(session: AsyncSession, *, user_id: int, business_id: int) -> tuple[PlayerBusiness, int]:
    if business_id not in BUSINESSES:
        raise BusinessError(f"Бизнес с номером {business_id} не существует (выберите 1..10)")

    pb = await session.scalar(
        select(PlayerBusiness).where(
            PlayerBusiness.user_id == user_id,
            PlayerBusiness.business_id == business_id,
        ).with_for_update()
    )
    if not pb:
        raise BusinessError("У вас еще нет этого бизнеса! Сначала купите его.")

    if pb.level >= 10:
        raise BusinessError("Бизнес уже улучшен до максимального (10) уровня!")

    cost = get_upgrade_cost(business_id, pb.level)
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(f"Недостаточно монет для улучшения: требуется {cost}, у вас {player.wallet}")

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Улучшение бизнеса #{business_id} до уровня {pb.level + 1}",
        operation_type="business_upgrade",
        reference=f"business:{business_id}",
    )

    pb.level += 1
    await session.flush()
    return pb, cost


async def collect_business_profits(session: AsyncSession, *, user_id: int) -> tuple[int, int]:
    """Собирает прибыль со всех бизнесов игрока. Возвращает (total_coins, count_businesses)."""
    pbs = await session.scalars(
        select(PlayerBusiness).where(PlayerBusiness.user_id == user_id).with_for_update()
    )
    businesses = list(pbs.all())
    if not businesses:
        return 0, 0

    now = datetime.now(timezone.utc)
    total_profit = 0

    for pb in businesses:
        last = pb.last_collected_at or now
        elapsed_hours = (now - last).total_seconds() / 3600.0
        # Бизнес приносит прибыль максимум за 4 часа работы без сбора!
        capped_hours = min(4.0, elapsed_hours)
        if capped_hours >= (1.0 / 60.0):
            income_per_hour = get_hourly_income(pb.business_id, pb.level)
            profit = int(income_per_hour * capped_hours)
            if profit > 0:
                total_profit += profit
                pb.last_collected_at = now

    if total_profit > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=total_profit,
            reason="Сбор прибыли с бизнесов",
            operation_type="business_profit",
        )
        await session.flush()

    return total_profit, len(businesses)
