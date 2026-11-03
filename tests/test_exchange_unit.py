import pytest
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import session_scope
from app.db.models import Player
from app.services.exchange_service import (
    create_limit_order,
    execute_market_order,
    cancel_order,
    get_order_book,
)

@pytest.mark.asyncio
async def test_limit_orders_and_matching():
    async with session_scope() as session:
        # Create seller and buyer with unique IDs
        seller = Player(telegram_id=9101, first_name="Seller", wallet=1000, shares=100)
        buyer = Player(telegram_id=9102, first_name="Buyer", wallet=1000, shares=0)
        session.add_all([seller, buyer])
        await session.flush()

        # Seller places limit sell: 50 shares @ 2.0
        sell_order, summary_sell = await create_limit_order(
            session, user_id=9101, side="sell", quantity=50, price="2.0"
        )
        assert sell_order.status == "open"
        assert seller.shares == 50
        assert summary_sell.filled_quantity == 0

        # Buyer places limit buy: 20 shares @ 2.0 -> Immediate partial match
        buy_order, summary_buy = await create_limit_order(
            session, user_id=9102, side="buy", quantity=20, price="2.0"
        )
        assert buy_order.status == "filled"
        assert summary_buy.filled_quantity == 20
        assert buyer.shares == 20
        assert sell_order.remaining_quantity == 30
        assert sell_order.status == "partially_filled"

        # Cancel remaining sell order
        cancelled = await cancel_order(session, order_id=sell_order.id, user_id=9101)
        assert cancelled.status == "cancelled"
        # 30 unfulfilled shares returned to seller: 50 + 30 = 80
        assert seller.shares == 80
