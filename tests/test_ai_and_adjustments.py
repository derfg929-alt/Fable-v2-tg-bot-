import pytest
from app.db.session import session_scope
from app.services.ai_service import solve_math_expression, get_ai_reply
from app.services.business_service import collect_business_profits, buy_business, BUSINESSES
from app.services.mining_service import set_mining_target, collect_mining_profit, buy_computers
from app.services.player_service import get_or_create_player


@pytest.mark.asyncio
async def test_ai_math_solver():
    # Test expression given by user: 6÷7*8÷99*68
    ans = solve_math_expression("сколько будет 6÷7*8÷99*68")
    assert ans is not None
    expected = (6 / 7 * 8 / 99 * 68)
    assert abs(float(ans) - expected) < 1e-4

    # Test full ai response
    uid = 999111
    async with session_scope() as session:
        reply = await get_ai_reply(session, chat_id=1001, user_id=uid, prompt="сколько будет 6÷7*8÷99*68")
        assert "Результат" in reply


@pytest.mark.asyncio
async def test_business_4_hour_cap():
    uid = 999112
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="BizCapTester")
        reg.player.wallet = 50000
        await session.flush()

        pb, cfg = await buy_business(session, user_id=uid, business_id=1)
        # Business 1 pays 6 coins/hour
        # Even if 24 hours pass, maximum collection is 4 hours = 24 coins
        from datetime import datetime, timedelta, timezone
        pb.last_collected_at = datetime.now(timezone.utc) - timedelta(hours=24)
        await session.flush()

        coins, count = await collect_business_profits(session, user_id=uid)
        assert count == 1
        assert coins <= 24  # capped at 4 hours * 6 coins/hr


@pytest.mark.asyncio
async def test_mining_target_crypto():
    uid = 999113
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="MiningTargetTester")
        reg.player.wallet = 100000
        await session.flush()

        # Buy computers and set mining to TON
        await buy_computers(session, user_id=uid, count=5)
        target = await set_mining_target(session, user_id=uid, ticker="TON")
        assert target == "TON"
