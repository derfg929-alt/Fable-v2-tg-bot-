"""Ядро биржи Fable: лимитные и рыночные заявки, Matching Engine.

Правила из ТЗ:
- Единый рынок для всех чатов и ЛС.
- Торговый актив: акции / единицы биржи (shares) за монеты (coins).
- Комиссия биржи: 1% при исполнении сделок.
- Приоритет исполнения: лучшая цена (для покупки выше, для продажи ниже), затем время (FIFO).
- Частичное исполнение разрешено.
- Атомарное изменение балансов и резервов.
- Блокировка/резервирование средств при создании лимитных заявок:
  * Покупка: резервируются монеты (цена * количество + комиссия).
  * Продажа: резервируются активы (shares).
- Отмена заявки возвращает только неиспользованный резерв.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Sequence

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    EconomyTransaction,
    MarketOrder,
    MarketQuote,
    MarketTrade,
    Player,
)
from app.services.economy_service import (
    EconomyError,
    InsufficientFundsError,
    InvalidAmountError,
    PlayerNotFoundError,
)


EXCHANGE_FEE_RATE = Decimal("0.01")  # 1% комиссия


class ExchangeError(Exception):
    """Базовая ошибка биржи."""


class InsufficientAssetsError(ExchangeError):
    """Недостаточно биржевых единиц/активов."""


class OrderNotFoundError(ExchangeError):
    """Заявка не найдена."""


class OrderAccessDeniedError(ExchangeError):
    """Попытка управления чужой заявкой."""


@dataclass(frozen=True, slots=True)
class OrderBookEntry:
    price: Decimal
    total_quantity: int
    order_count: int


@dataclass(frozen=True, slots=True)
class OrderBook:
    bids: list[OrderBookEntry]  # Покупатели (от высокой цены к низкой)
    asks: list[OrderBookEntry]  # Продавцы (от низкой цены к высокой)
    last_price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    daily_volume: int
    daily_trade_count: int
    price_change_24h_percent: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionSummary:
    order: MarketOrder
    filled_quantity: int
    total_cost_coins: int
    total_fee_coins: int
    trades: list[MarketTrade]


def _to_decimal_price(price: Decimal | float | int | str) -> Decimal:
    try:
        dec = Decimal(str(price)).quantize(Decimal("0.00000001"))
        if dec <= Decimal("0"):
            raise ValueError
        return dec
    except Exception:
        raise ExchangeError("Цена должна быть положительным числовым значением")


async def get_current_market_price(session: AsyncSession) -> Decimal:
    """Возвращает цену последней сделки или 1.00 по умолчанию."""
    last_trade = await session.scalar(
        select(MarketTrade.price).order_by(desc(MarketTrade.id)).limit(1)
    )
    if last_trade is not None:
        return Decimal(str(last_trade))
    last_quote = await session.scalar(
        select(MarketQuote.price).order_by(desc(MarketQuote.id)).limit(1)
    )
    if last_quote is not None:
        return Decimal(str(last_quote))
    return Decimal("1.00")


async def get_order_book(session: AsyncSession, depth: int = 10) -> OrderBook:
    """Возвращает текущий стакан заявок и статистику биржи."""
    # Bids (покупка)
    bids_res = await session.execute(
        select(
            MarketOrder.limit_price,
            func.sum(MarketOrder.remaining_quantity).label("total_qty"),
            func.count(MarketOrder.id).label("cnt"),
        )
        .where(
            MarketOrder.side == "buy",
            MarketOrder.status.in_(["open", "partially_filled"]),
            MarketOrder.remaining_quantity > 0,
        )
        .group_by(MarketOrder.limit_price)
        .order_by(desc(MarketOrder.limit_price))
        .limit(depth)
    )
    bids = [
        OrderBookEntry(
            price=Decimal(str(r[0])), total_quantity=int(r[1]), order_count=int(r[2])
        )
        for r in bids_res.all()
    ]

    # Asks (продажа)
    asks_res = await session.execute(
        select(
            MarketOrder.limit_price,
            func.sum(MarketOrder.remaining_quantity).label("total_qty"),
            func.count(MarketOrder.id).label("cnt"),
        )
        .where(
            MarketOrder.side == "sell",
            MarketOrder.status.in_(["open", "partially_filled"]),
            MarketOrder.remaining_quantity > 0,
        )
        .group_by(MarketOrder.limit_price)
        .order_by(MarketOrder.limit_price.asc())
        .limit(depth)
    )
    asks = [
        OrderBookEntry(
            price=Decimal(str(r[0])), total_quantity=int(r[1]), order_count=int(r[2])
        )
        for r in asks_res.all()
    ]

    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    spread = (best_ask - best_bid) if (best_bid and best_ask) else None
    last_price = await get_current_market_price(session)

    # 24h stats
    since_24h = datetime.now(timezone.utc) - func.make_interval(0, 0, 0, 0, 24, 0, 0)
    stats = await session.execute(
        select(
            func.coalesce(func.sum(MarketTrade.quantity), 0),
            func.count(MarketTrade.id),
        ).where(MarketTrade.created_at >= since_24h)
    )
    daily_vol_row = stats.first()
    daily_volume = int(daily_vol_row[0]) if daily_vol_row else 0
    daily_trade_count = int(daily_vol_row[1]) if daily_vol_row else 0

    first_price_24h = await session.scalar(
        select(MarketTrade.price)
        .where(MarketTrade.created_at >= since_24h)
        .order_by(MarketTrade.id.asc())
        .limit(1)
    )
    if first_price_24h and Decimal(str(first_price_24h)) > Decimal("0"):
        first_p = Decimal(str(first_price_24h))
        change_pct = ((last_price - first_p) / first_p) * Decimal("100")
    else:
        change_pct = Decimal("0")

    return OrderBook(
        bids=bids,
        asks=asks,
        last_price=last_price,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        daily_volume=daily_volume,
        daily_trade_count=daily_trade_count,
        price_change_24h_percent=change_pct.quantize(Decimal("0.1")),
    )


async def create_limit_order(
    session: AsyncSession,
    *,
    user_id: int | None,
    side: str,  # 'buy' or 'sell'
    quantity: int,
    price: Decimal | float | int | str,
    is_market_maker: bool = False,
    idempotency_key: str | None = None,
) -> tuple[MarketOrder, ExecutionSummary]:
    """Создаёт лимитную заявку, резервирует баланс/актив и запускает Matching Engine."""
    if quantity <= 0:
        raise InvalidAmountError("Количество должно быть положительным целым числом")
    side = side.lower().strip()
    if side not in ("buy", "sell"):
        raise ValueError("Сторона заявки должна быть 'buy' или 'sell'")

    dec_price = _to_decimal_price(price)

    # Идемпотентность
    if idempotency_key:
        existing = await session.scalar(
            select(MarketOrder).where(MarketOrder.idempotency_key == idempotency_key)
        )
        if existing:
            return existing, ExecutionSummary(existing, 0, 0, 0, [])

    player: Player | None = None
    reserved_coins = 0
    reserved_assets = 0

    if user_id is not None:
        player = await session.scalar(
            select(Player).where(Player.telegram_id == user_id).with_for_update()
        )
        if not player:
            raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

        if side == "buy":
            # Резервируем монеты: количество * цена + 1% комиссия
            est_cost = Decimal(quantity) * dec_price
            est_fee = (est_cost * EXCHANGE_FEE_RATE).to_integral_value(rounding=ROUND_UP)
            required_coins = int(est_cost.to_integral_value(rounding=ROUND_UP) + est_fee)
            if player.wallet < required_coins:
                raise InsufficientFundsError(
                    f"Недостаточно монет для заявки. Требуется {required_coins}, доступно {player.wallet}"
                )
            player.wallet -= required_coins
            reserved_coins = required_coins
        else:
            # Резервируем акции / shares
            if player.shares < quantity:
                raise InsufficientAssetsError(
                    f"Недостаточно биржевых единиц. Требуется {quantity}, доступно {player.shares}"
                )
            player.shares -= quantity
            reserved_assets = quantity

    order = MarketOrder(
        user_id=user_id,
        side=side,
        order_type="limit",
        quantity=quantity,
        remaining_quantity=quantity,
        limit_price=dec_price,
        reserved_coins=reserved_coins,
        reserved_assets=reserved_assets,
        status="open",
        is_market_maker=is_market_maker,
        idempotency_key=idempotency_key,
    )
    session.add(order)
    await session.flush()

    # Запускаем Matching Engine
    summary = await _match_order(session, order)
    return order, summary


async def execute_market_order(
    session: AsyncSession,
    *,
    user_id: int,
    side: str,  # 'buy' or 'sell'
    quantity: int,
) -> ExecutionSummary:
    """Рыночная покупка или продажа по лучшим доступным ценам в стакане."""
    if quantity <= 0:
        raise InvalidAmountError("Количество должно быть положительным целым числом")
    side = side.lower().strip()
    if side not in ("buy", "sell"):
        raise ValueError("Сторона заявки должна быть 'buy' или 'sell'")

    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    # Создаём псевдо-ордер с экстремальной ценой для сбора стакана
    if side == "buy":
        # Максимальная цена
        extreme_price = Decimal("999999999.00000000")
        # Для рыночной покупки резервируем имеющийся кошелек
        max_possible_coins = player.wallet
        if max_possible_coins <= 0:
            raise InsufficientFundsError("На кошельке нет монет для покупки")
        order = MarketOrder(
            user_id=user_id,
            side="buy",
            order_type="market",
            quantity=quantity,
            remaining_quantity=quantity,
            limit_price=extreme_price,
            reserved_coins=0,  # списываем по факту исполнения
            reserved_assets=0,
            status="open",
            is_market_maker=False,
        )
    else:
        if player.shares < quantity:
            raise InsufficientAssetsError(
                f"Недостаточно биржевых единиц: доступно {player.shares}, требуется {quantity}"
            )
        player.shares -= quantity
        order = MarketOrder(
            user_id=user_id,
            side="sell",
            order_type="market",
            quantity=quantity,
            remaining_quantity=quantity,
            limit_price=Decimal("0.00000001"),
            reserved_coins=0,
            reserved_assets=quantity,
            status="open",
            is_market_maker=False,
        )

    session.add(order)
    await session.flush()

    summary = await _match_order(session, order, is_market=True)

    # Если рыночный ордер остался частично неисполненным из-за отсутствия ликвидности
    if order.remaining_quantity > 0:
        unfilled = order.remaining_quantity
        order.status = "filled" if summary.filled_quantity > 0 else "cancelled"
        if side == "sell":
            # Возвращаем неисполненные акции
            player.shares += unfilled
            order.reserved_assets -= unfilled

    await session.flush()
    return summary


async def cancel_order(
    session: AsyncSession,
    *,
    order_id: int,
    user_id: int,
) -> MarketOrder:
    """Отменяет активную заявку игрока и возвращает неиспользованный резерв."""
    order = await session.scalar(
        select(MarketOrder).where(MarketOrder.id == order_id).with_for_update()
    )
    if not order:
        raise OrderNotFoundError(f"Заявка #{order_id} не найдена")

    if order.user_id != user_id:
        raise OrderAccessDeniedError("Вы не можете отменить чужую заявку")

    if order.status not in ("open", "partially_filled") or order.remaining_quantity <= 0:
        raise ExchangeError(f"Заявка #{order_id} уже закрыта со статусом {order.status}")

    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if not player:
        raise PlayerNotFoundError(f"Игрок с ID {user_id} не найден")

    # Возврат неиспользованного резерва
    if order.side == "buy" and order.reserved_coins > 0:
        player.wallet += order.reserved_coins
        order.reserved_coins = 0
    elif order.side == "sell" and order.reserved_assets > 0:
        player.shares += order.reserved_assets
        order.reserved_assets = 0

    order.status = "cancelled"
    await session.flush()
    return order


async def get_user_active_orders(
    session: AsyncSession, user_id: int
) -> list[MarketOrder]:
    """Возвращает список активных заявок пользователя."""
    res = await session.scalars(
        select(MarketOrder)
        .where(
            MarketOrder.user_id == user_id,
            MarketOrder.status.in_(["open", "partially_filled"]),
            MarketOrder.remaining_quantity > 0,
        )
        .order_by(MarketOrder.created_at.desc())
    )
    return list(res.all())


async def _match_order(
    session: AsyncSession,
    incoming_order: MarketOrder,
    is_market: bool = False,
) -> ExecutionSummary:
    """Matching Engine: сведение входящей заявки с противоположной стороной стакана."""
    filled_quantity = 0
    total_cost_coins = 0
    total_fee_coins = 0
    trades: list[MarketTrade] = []

    opposite_side = "sell" if incoming_order.side == "buy" else "buy"

    # Запрашиваем встречные заявки по лучшей цене и дате (FIFO)
    query = (
        select(MarketOrder)
        .where(
            MarketOrder.side == opposite_side,
            MarketOrder.status.in_(["open", "partially_filled"]),
            MarketOrder.remaining_quantity > 0,
            MarketOrder.id != incoming_order.id,
        )
        .with_for_update()
    )

    if incoming_order.side == "buy":
        # Покупаем у продавцов: берем самую дешевую продажу <= лимита
        if not is_market:
            query = query.where(MarketOrder.limit_price <= incoming_order.limit_price)
        query = query.order_by(MarketOrder.limit_price.asc(), MarketOrder.created_at.asc())
    else:
        # Продаем покупателям: берем самую дорогую покупку >= лимита
        if not is_market:
            query = query.where(MarketOrder.limit_price >= incoming_order.limit_price)
        query = query.order_by(desc(MarketOrder.limit_price), MarketOrder.created_at.asc())

    maker_orders = (await session.scalars(query)).all()

    # Предзагружаем участников для обновления балансов
    user_ids_to_lock = {
        m.user_id for m in maker_orders if m.user_id is not None
    }
    if incoming_order.user_id:
        user_ids_to_lock.add(incoming_order.user_id)

    players_map: dict[int, Player] = {}
    if user_ids_to_lock:
        players_res = await session.scalars(
            select(Player)
            .where(Player.telegram_id.in_(sorted(user_ids_to_lock)))
            .order_by(Player.telegram_id)
            .with_for_update()
        )
        players_map = {p.telegram_id: p for p in players_res.all()}

    for maker in maker_orders:
        if incoming_order.remaining_quantity <= 0:
            break

        match_qty = min(incoming_order.remaining_quantity, maker.remaining_quantity)
        trade_price = maker.limit_price  # Цена сделки определяется существующим в стакане мейкером

        gross_cost = (Decimal(match_qty) * trade_price).to_integral_value(rounding=ROUND_UP)
        gross_cost_int = int(gross_cost)
        fee = int((Decimal(gross_cost_int) * EXCHANGE_FEE_RATE).to_integral_value(rounding=ROUND_UP))

        buy_order = incoming_order if incoming_order.side == "buy" else maker
        sell_order = maker if incoming_order.side == "buy" else incoming_order

        buyer = players_map.get(buy_order.user_id) if buy_order.user_id else None
        seller = players_map.get(sell_order.user_id) if sell_order.user_id else None

        # Проверка баланса при рыночной покупке
        if incoming_order.side == "buy" and is_market and buyer:
            needed = gross_cost_int + fee
            if buyer.wallet < needed:
                # Покупаем сколько хватает
                max_affordable_qty = int(
                    (Decimal(buyer.wallet) / (trade_price * (Decimal("1") + EXCHANGE_FEE_RATE)))
                )
                if max_affordable_qty <= 0:
                    break
                match_qty = min(match_qty, max_affordable_qty)
                gross_cost_int = int((Decimal(match_qty) * trade_price).to_integral_value(rounding=ROUND_UP))
                fee = int((Decimal(gross_cost_int) * EXCHANGE_FEE_RATE).to_integral_value(rounding=ROUND_UP))

        # Выполняем перевод активов и монет
        # 1. Покупатель получает shares, списываются монеты
        if buyer:
            buyer.shares += match_qty
            if buy_order == incoming_order and not is_market:
                # Монеты уже были зарезервированы в reserved_coins
                # Списываем фактическую стоимость и комиссию из резерва
                total_deduct = gross_cost_int + fee
                # Если фактическая цена оказалась лучше (ниже) лимитной, излишек резерва вернется
                buy_order.reserved_coins = max(0, buy_order.reserved_coins - total_deduct)
            else:
                buyer.wallet -= (gross_cost_int + fee)

        # 2. Продавец отдает shares (уже зарезервированы), получает монеты за вычетом комиссии
        seller_proceeds = gross_cost_int - fee
        if seller:
            seller.wallet += max(0, seller_proceeds)
            sell_order.reserved_assets = max(0, sell_order.reserved_assets - match_qty)

        # Обновляем остатки ордеров
        incoming_order.remaining_quantity -= match_qty
        maker.remaining_quantity -= match_qty

        if maker.remaining_quantity == 0:
            maker.status = "filled"
            # Если остался неиспользованный резерв из-за разницы цен, возвращаем его
            if maker.side == "buy" and maker.reserved_coins > 0 and seller:
                maker_player = players_map.get(maker.user_id) if maker.user_id else None
                if maker_player:
                    maker_player.wallet += maker.reserved_coins
                maker.reserved_coins = 0
            elif maker.side == "sell" and maker.reserved_assets > 0:
                maker.reserved_assets = 0
        else:
            maker.status = "partially_filled"

        trade = MarketTrade(
            buy_order_id=buy_order.id,
            sell_order_id=sell_order.id,
            buyer_id=buy_order.user_id,
            seller_id=sell_order.user_id,
            quantity=match_qty,
            price=trade_price,
            buyer_fee=fee,
            seller_fee=fee,
        )
        session.add(trade)
        trades.append(trade)

        filled_quantity += match_qty
        total_cost_coins += gross_cost_int
        total_fee_coins += fee

    if incoming_order.remaining_quantity == 0:
        incoming_order.status = "filled"
        # Возвращаем остаток резерва, если цена покупки была ниже лимитной
        if incoming_order.side == "buy" and incoming_order.reserved_coins > 0:
            buyer_player = players_map.get(incoming_order.user_id) if incoming_order.user_id else None
            if buyer_player:
                buyer_player.wallet += incoming_order.reserved_coins
            incoming_order.reserved_coins = 0
    elif filled_quantity > 0:
        incoming_order.status = "partially_filled"

    await session.flush()
    return ExecutionSummary(
        order=incoming_order,
        filled_quantity=filled_quantity,
        total_cost_coins=total_cost_coins,
        total_fee_coins=total_fee_coins,
        trades=trades,
    )
