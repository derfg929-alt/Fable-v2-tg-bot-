"""Регрессионные тесты: команда "баланс" и балансировка азартных игр.

1. Команда "баланс" ранее падала с NameError (select не был импортирован).
2. Казино-игры были перенастроены на house edge ~10% (RTP ~90%).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.handlers.economy_handlers import handle_balance
from app.services.games_service import (
    play_dice,
    play_coinflip,
    play_mystery_box,
    play_crash_game,
    play_shooting_range_game,
    play_slots,
    play_roulette,
)


@pytest.mark.asyncio
async def test_balance_command_does_not_crash():
    """Команда "баланс" должна отвечать, а не падать с NameError."""
    uid = 91827364
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="BalanceTester")
        reg.player.wallet = 1234
        reg.player.bank_balance = 567

    msg = MagicMock()
    msg.from_user = MagicMock(id=uid, first_name="BalanceTester", last_name=None, username=None)
    msg.reply = AsyncMock()

    await handle_balance(msg)

    assert msg.reply.called
    text = msg.reply.call_args[0][0]
    assert "ФИНАНСОВЫЙ БАЛАНС" in text


@pytest.mark.asyncio
async def test_casino_games_have_house_edge():
    """Каждая игра должна иметь преимущество казино (RTP заметно ниже 100%)."""
    import random as _random

    _random.seed(20240101)
    uid = 42424200
    stake = 100
    rounds = 1200
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="RtpTester")
        reg.player.wallet = 10 ** 12
        await session.flush()

        async def measure(fn, **kwargs) -> float:
            reg.player.wallet = 10 ** 12
            await session.flush()
            staked = 0
            paid = 0
            for _ in range(rounds):
                out = await fn(session, user_id=uid, stake=stake, **kwargs)
                payout = out[-1] if isinstance(out, tuple) else out.payout
                staked += stake
                paid += payout
            return paid / staked

        rtp_dice = await measure(play_dice)
        rtp_coin = await measure(play_coinflip, choice="орел")
        rtp_box = await measure(play_mystery_box)
        rtp_crash = await measure(play_crash_game)
        rtp_shoot = await measure(play_shooting_range_game)
        rtp_slots = await measure(play_slots)
        rtp_roulette = await measure(play_roulette, bet_target="red")

    # При фиксированном seed результат детерминирован. Все игры нацелены на RTP ~0.90.
    for name, rtp in [
        ("dice", rtp_dice),
        ("coinflip", rtp_coin),
        ("mystery_box", rtp_box),
        ("crash", rtp_crash),
        ("shooting", rtp_shoot),
        ("slots", rtp_slots),
        ("roulette", rtp_roulette),
    ]:
        assert 0.78 <= rtp <= 1.0, f"{name}: неожиданный RTP {rtp:.3f}"
        # Казино не должно в среднем возвращать игроку больше ставки.
        assert rtp < 1.0, f"{name}: house edge отсутствует (RTP {rtp:.3f})"
