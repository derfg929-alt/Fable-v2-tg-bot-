"""Автоматический маркетмейкер биржи Fable.

Правила из ТЗ:
- Общий автоматический участник рынка для обеспечения ликвидности.
- Выставляет заявки на покупку и продажу с заданным спредом.
- Изменяет цены с учетом реального спроса и предложения:
  * Если игроков, желающих продать, становится значительно больше, маркетмейкер снижает цену покупки.
  * Если покупателей становится значительно больше, маркетмейкер повышает цену продажи.
- Использует собственные резервы (coin_reserve, asset_reserve).
- Прекращает выставление заявок, если резервов недостаточно.
- Настраиваемые параметры: начальная цена, спред, чувствительность, резервы, макс. объем одной заявки.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import logging

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketMakerState, MarketOrder, MarketQuote, MarketTrade
from app.services.exchange_service import create_limit_order

logger = logging.getLogger(__name__)


async def get_or_create_mm_state(session: AsyncSession) -> MarketMakerState:
    """Возвращает или инициализирует глобальное состояние маркетмейкера."""
    state = await session.get(MarketMakerState, 1)
    if not state:
        state = MarketMakerState(
            id=1,
            enabled=True,
            reference_price=Decimal("1.00"),
            spread=Decimal("0.04"),  # 4% спред (+-2% от базовой цены)
            coin_reserve=10_000_000,
            asset_reserve=10_000_000,
            parameters={
                "order_size": 100,
                "sensitivity": 0.005,  # сдвиг цены на 0.5% за дисбаланс
                "max_orders_per_side": 3,
                "step_percent": 0.01,
            },
        )
        session.add(state)
        await session.flush()
    return state


async def run_market_maker_cycle(session: AsyncSession) -> bool:
    """Выполняет один цикл обновления котировок и заявок маркетмейкера."""
    state = await get_or_create_mm_state(session)
    if not state.enabled:
        return False

    params = state.parameters or {}
    order_size = int(params.get("order_size", 100))
    sensitivity = Decimal(str(params.get("sensitivity", "0.005")))
    max_orders_per_side = int(params.get("max_orders_per_side", 3))
    step_pct = Decimal(str(params.get("step_percent", "0.01")))

    # 1. Анализируем спрос и предложение в реальном стакане (без заявок MM)
    demand_qty = (
        await session.scalar(
            select(func.coalesce(func.sum(MarketOrder.remaining_quantity), 0)).where(
                MarketOrder.side == "buy",
                MarketOrder.status.in_(["open", "partially_filled"]),
                MarketOrder.is_market_maker.is_(False),
            )
        )
        or 0
    )
    supply_qty = (
        await session.scalar(
            select(func.coalesce(func.sum(MarketOrder.remaining_quantity), 0)).where(
                MarketOrder.side == "sell",
                MarketOrder.status.in_(["open", "partially_filled"]),
                MarketOrder.is_market_maker.is_(False),
            )
        )
        or 0
    )

    # 2. Корректируем reference_price в зависимости от дисбаланса
    # Если спрос > предложения -> повышаем цену, если предложение > спроса -> снижаем
    ref_price = Decimal(str(state.reference_price))
    if demand_qty > supply_qty:
        imbalance_ratio = min(Decimal("0.10"), (Decimal(demand_qty - supply_qty) / Decimal(max(demand_qty + supply_qty, 1))) * sensitivity)
        ref_price = ref_price * (Decimal("1") + imbalance_ratio)
    elif supply_qty > demand_qty:
        imbalance_ratio = min(Decimal("0.10"), (Decimal(supply_qty - demand_qty) / Decimal(max(demand_qty + supply_qty, 1))) * sensitivity)
        ref_price = ref_price * (Decimal("1") - imbalance_ratio)

    ref_price = max(Decimal("0.01"), ref_price.quantize(Decimal("0.0001")))
    state.reference_price = ref_price

    # 3. Снимаем старые неисполненные заявки маркетмейкера
    mm_orders = (
        await session.scalars(
            select(MarketOrder).where(
                MarketOrder.is_market_maker.is_(True),
                MarketOrder.status.in_(["open", "partially_filled"]),
            )
        )
    ).all()
    for mo in mm_orders:
        mo.status = "cancelled"

    # 4. Выставляем новые заявки вокруг reference_price с учетом спреда
    half_spread = (Decimal(str(state.spread)) / Decimal("2"))
    base_bid = ref_price * (Decimal("1") - half_spread)
    base_ask = ref_price * (Decimal("1") + half_spread)

    # Ставим несколько уровней bids и asks
    for i in range(max_orders_per_side):
        bid_price = max(
            Decimal("0.01"),
            (base_bid * (Decimal("1") - (step_pct * Decimal(i)))).quantize(Decimal("0.0001")),
        )
        ask_price = (base_ask * (Decimal("1") + (step_pct * Decimal(i)))).quantize(Decimal("0.0001"))

        # Проверяем резервы MM
        needed_coins = int((Decimal(order_size) * bid_price * Decimal("1.01")).to_integral_value(rounding=ROUND_UP))
        if state.coin_reserve >= needed_coins:
            await create_limit_order(
                session,
                user_id=None,
                side="buy",
                quantity=order_size,
                price=bid_price,
                is_market_maker=True,
            )
            state.coin_reserve -= needed_coins

        if state.asset_reserve >= order_size:
            await create_limit_order(
                session,
                user_id=None,
                side="sell",
                quantity=order_size,
                price=ask_price,
                is_market_maker=True,
            )
            state.asset_reserve -= order_size

    # 5. Сохраняем текущую котировку в MarketQuote
    quote = MarketQuote(
        price=ref_price,
        best_bid=base_bid.quantize(Decimal("0.0001")),
        best_ask=base_ask.quantize(Decimal("0.0001")),
        volume=int(demand_qty + supply_qty),
        demand=int(demand_qty),
        supply=int(supply_qty),
    )
    session.add(quote)
    await session.flush()
    return True
