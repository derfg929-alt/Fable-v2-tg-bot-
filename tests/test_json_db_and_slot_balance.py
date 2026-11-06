import pytest
import os
import json
from app.db.session import session_scope
from app.db.json_db import json_db_manager
from app.services.player_service import get_or_create_player
from app.services.games_service import play_slots, InvalidAmountError

@pytest.mark.asyncio
async def test_json_database_persistence():
    uid = 55667788
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="JsonTester")
        reg.player.wallet = 7777

    # Verify that data exists in json_db_manager
    json_data = json_db_manager.load_json()
    assert "players" in json_data
    players = json_data["players"]
    found = [p for p in players if p.get("telegram_id") == uid]
    assert len(found) == 1
    assert found[0]["wallet"] == 7777

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
