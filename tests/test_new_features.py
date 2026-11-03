import pytest
from decimal import Decimal
from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.bank_service import deposit, withdraw, get_bank_status
from app.services.business_service import buy_business, upgrade_business, collect_business_profits, get_player_businesses
from app.services.mining_service import get_or_create_mining, buy_computers, upgrade_mining_farm, collect_mining_profit
from app.services.crypto_stock_service import buy_asset, sell_asset, get_player_portfolio
from app.services.games_service import claim_daily_bonus, play_dice, play_coinflip, play_mystery_box


@pytest.mark.asyncio
async def test_bank_operations_reduced_rate():
    uid = 999001
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="BankTester")
        reg.player.wallet = 10000
        await session.flush()

        # Deposit
        dep_res = await deposit(session, user_id=uid, amount=4000)
        assert dep_res.wallet_balance == 6000
        assert dep_res.bank_balance == 4000

        # Withdraw
        with_res = await withdraw(session, user_id=uid, amount=1000)
        assert with_res.wallet_balance == 7000
        assert with_res.bank_balance == 3000

        status = await get_bank_status(session, user_id=uid)
        assert status["interest_rate"] == Decimal("0.01")  # Reduced rate <= 10000


@pytest.mark.asyncio
async def test_business_and_mining():
    uid = 999002
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="BizTester")
        reg.player.wallet = 500000
        await session.flush()

        # Buy and upgrade Shawarma
        pb, cfg = await buy_business(session, user_id=uid, business_id=1)
        assert pb.level == 1
        pb_upg, cost = await upgrade_business(session, user_id=uid, business_id=1)
        assert pb_upg.level == 2

        b_list = await get_player_businesses(session, user_id=uid)
        assert len(b_list) == 1

        # Mining
        mining = await get_or_create_mining(session, user_id=uid)
        assert mining.computers == 1
        m_after, b_cost = await buy_computers(session, user_id=uid, count=2)
        assert m_after.computers == 3
        m_upg, upg_cost = await upgrade_mining_farm(session, user_id=uid)
        assert m_upg.farm_level == 2


@pytest.mark.asyncio
async def test_crypto_stock_portfolio():
    uid = 999003
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="CryptoTester")
        reg.player.wallet = 10000000
        await session.flush()

        # Buy BTC
        p_btc, c_btc = await buy_asset(session, user_id=uid, ticker="BTC", quantity=Decimal("0.5"))
        assert c_btc > 0

        # Buy AAPL
        p_aapl, c_aapl = await buy_asset(session, user_id=uid, ticker="AAPL", quantity=Decimal("10"))
        assert c_aapl > 0

        portfolio = await get_player_portfolio(session, user_id=uid)
        tickers = [item[0].ticker for item in portfolio]
        assert "BTC" in tickers
        assert "AAPL" in tickers

        # Sell some BTC
        p_sell, rev_btc = await sell_asset(session, user_id=uid, ticker="BTC", quantity=Decimal("0.2"))
        assert rev_btc > 0


@pytest.mark.asyncio
async def test_bonus_and_minigames():
    uid = 999004
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="GameTester")
        reg.player.wallet = 10000
        await session.flush()

        # Daily bonus
        bon = await claim_daily_bonus(session, user_id=uid)
        assert bon.reward_coins == 50
        assert bon.streak == 1

        # Dice
        _, _, _, dice_payout = await play_dice(session, user_id=uid, stake=100)
        assert dice_payout >= 0

        # Coinflip
        _, _, coin_payout = await play_coinflip(session, user_id=uid, choice="орел", stake=100)
        assert coin_payout >= 0

        # Mystery box
        mult, box_payout = await play_mystery_box(session, user_id=uid, stake=100)
        assert mult >= 0


@pytest.mark.asyncio
async def test_business_4_hour_accumulation_cap():
    from datetime import datetime, timezone, timedelta
    from app.services.business_service import MAX_BUSINESS_ACCUMULATION_HOURS
    from app.db.models import PlayerBusiness

    uid = 999005
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="CapTester")
        reg.player.wallet = 50000
        await session.flush()

        pb, cfg = await buy_business(session, user_id=uid, business_id=1)
        # Симулируем 10 часов отсутствия
        ten_hours_ago = datetime.now(timezone.utc) - timedelta(hours=10)
        pb.last_collected_at = ten_hours_ago
        await session.flush()

        # Собираем прибыль
        profit, count = await collect_business_profits(session, user_id=uid)
        assert count == 1
        # Прибыль должна быть ровно за 4 часа, а не за 10
        expected_max_profit = int(15 * MAX_BUSINESS_ACCUMULATION_HOURS)
        assert profit == expected_max_profit


@pytest.mark.asyncio
async def test_multi_crypto_mining_rigs():
    from app.services.mining_service import (
        switch_mining_coin,
        collect_mining_profit_detailed,
        get_rigs_dict,
    )
    from datetime import datetime, timezone, timedelta

    uid = 999006
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="MinerTester")
        reg.player.wallet = 100000
        await session.flush()

        # Buy GPU rig
        m, cost = await buy_computers(session, user_id=uid, count=1, rig_type="gpu")
        rigs = get_rigs_dict(m)
        assert rigs.get("gpu") == 1

        # Switch to ETH
        active = await switch_mining_coin(session, user_id=uid, coin="ETH")
        assert active == "ETH"

        # Simulate 2 hours elapsed
        m.last_mined_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.flush()

        res = await collect_mining_profit_detailed(session, user_id=uid)
        assert res.crypto_coin == "ETH"
        assert res.crypto_amount > 0


@pytest.mark.asyncio
async def test_engagement_service_baits():
    from app.services.engagement_service import generate_chat_bait_message
    text, kb = await generate_chat_bait_message()
    assert text
    assert kb.inline_keyboard[0][0].callback_data == "bait:claim"
