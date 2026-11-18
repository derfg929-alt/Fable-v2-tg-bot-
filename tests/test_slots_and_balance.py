import pytest

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.games_service import play_slots, InvalidAmountError


@pytest.mark.asyncio
async def test_sqlite_database_persistence():
    """Данные сохраняются между сессиями в едином SQLite-хранилище."""
    uid = 55667788
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="SqliteTester")
        reg.player.wallet = 7777

    # Новая сессия видит те же данные (долговременное хранилище — SQLite).
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="SqliteTester")
        assert reg.player.wallet == 7777


@pytest.mark.asyncio
async def test_slots_rebalance_and_limit():
    uid = 99884422
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="SlotBalanced")
        reg.player.wallet = 10_000_000

        # Attempting to bet more than 50,000 raises InvalidAmountError
        with pytest.raises(InvalidAmountError):
            await play_slots(session, user_id=uid, stake=100_000)

        # Normal bet (e.g., 500) works smoothly
        res = await play_slots(session, user_id=uid, stake=500)
        assert len(res.reels) == 3
        assert res.multiplier >= 0.0
