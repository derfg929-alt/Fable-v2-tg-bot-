"""Тесты механик роста Fable Randomizer: билеты, кодовые слова, задания
спонсоров, рефералы «за действие», рейтинг, источники, прозрачность розыгрыша,
запасные победители и CSV-экспорт.

Все проверки идут через реальные сервисы и реальную БД (SQLite в памяти).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import Referral
from app.db.session import session_scope
from app.services import contest_referral_service as crefs
from app.services import contest_service as cs
from app.services import contest_settings as cset
from app.services import draw_service as ds
from app.services import leaderboard_service as lb
from app.services import ticket_service as tickets
from app.services.player_service import get_or_create_player


class FakeBot:
    """Бот, который всегда подтверждает подписку (нет обязательных каналов)."""

    async def get_chat_member(self, *args, **kwargs):
        return MagicMock(status="member")

    async def send_message(self, *args, **kwargs):
        return MagicMock(message_id=1)


async def _player(uid: int):
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name=f"U{uid}")
        return reg.player


async def _make_contest(**settings) -> int:
    async with session_scope() as session:
        contest = await cs.create_contest(
            session,
            title="Growth Contest",
            prize="Prize",
            winners_count=1,
            status=cs.STATUS_ACTIVE,
            settings=settings or None,
        )
        return contest.id


# ===================== БИЛЕТЫ =====================

@pytest.mark.asyncio
async def test_base_ticket_on_join_and_idempotency():
    contest_id = await _make_contest()
    bot = FakeBot()
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=1001, first_name="A")
    async with session_scope() as session:
        entry = await tickets.get_entry(session, contest_id, 1001)
        assert int(entry.tickets) == 1
        # Повторное начисление базового билета не удваивает счётчик.
        await tickets.ensure_base_ticket(session, contest_id=contest_id, user_id=1001)
    async with session_scope() as session:
        entry = await tickets.get_entry(session, contest_id, 1001)
        assert int(entry.tickets) == 1


@pytest.mark.asyncio
async def test_code_word_awards_tickets_once():
    contest_id = await _make_contest(
        code_words=True,
        code_words_list=[{"word": "FABLE", "tickets": 3}],
    )
    bot = FakeBot()
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=1002, first_name="A")

    async with session_scope() as session:
        res = await tickets.redeem_code_word(session, contest_id=contest_id, user_id=1002, text="fable")
        assert res.status == "ok" and res.tickets == 3
    async with session_scope() as session:
        # Повторный ввод того же слова не начисляет билеты.
        res = await tickets.redeem_code_word(session, contest_id=contest_id, user_id=1002, text="FABLE")
        assert res.status == "already"
    async with session_scope() as session:
        entry = await tickets.get_entry(session, contest_id, 1002)
        assert int(entry.tickets) == 4  # 1 базовый + 3 за слово
        breakdown = await tickets.get_ticket_breakdown(session, contest_id, 1002)
        assert breakdown[tickets.REASON_CODE_WORD] == 3


@pytest.mark.asyncio
async def test_code_word_wrong_and_disabled():
    contest_id = await _make_contest(code_words=True, code_words_list=[{"word": "SECRET", "tickets": 2}])
    bot = FakeBot()
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=1003, first_name="A")
    async with session_scope() as session:
        res = await tickets.redeem_code_word(session, contest_id=contest_id, user_id=1003, text="WRONG")
        assert res.status == "not_found"

    plain_id = await _make_contest()
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=plain_id, user_id=1003, first_name="A")
    async with session_scope() as session:
        res = await tickets.redeem_code_word(session, contest_id=plain_id, user_id=1003, text="X")
        assert res.status == "disabled"


@pytest.mark.asyncio
async def test_sponsor_task_awards_once():
    contest_id = await _make_contest(sponsor_tasks=True, sponsor_task_text="do it", sponsor_task_tickets=5)
    bot = FakeBot()
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=1004, first_name="A")
    async with session_scope() as session:
        assert await tickets.submit_sponsor_task(session, contest_id=contest_id, user_id=1004) is True
    async with session_scope() as session:
        assert await tickets.submit_sponsor_task(session, contest_id=contest_id, user_id=1004) is False
    async with session_scope() as session:
        entry = await tickets.get_entry(session, contest_id, 1004)
        assert int(entry.tickets) == 6  # 1 базовый + 5 за задание


# ===================== РЕФЕРАЛЫ «ЗА ДЕЙСТВИЕ» =====================

@pytest.mark.asyncio
async def test_referral_credited_only_after_action():
    """Друг, приведённый по ссылке, приносит билеты только после выполнения условий."""
    contest_id = await _make_contest(
        code_words=True,
        code_words_list=[{"word": "CODE", "tickets": 1}],
        tickets_per_ref=5,
    )
    inviter, referred = 2001, 2002
    await _player(inviter)
    await _player(referred)
    async with session_scope() as session:
        session.add(Referral(inviter_id=inviter, invited_id=referred))
        await session.flush()

    bot = FakeBot()
    # Оба участвуют.
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=inviter, first_name="Inv")
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=referred, first_name="Ref")

    # У конкурса есть условие (кодовое слово) — реферал пока НЕ зачтён.
    async with session_scope() as session:
        assert await crefs.count_referrals_for(session, contest_id, inviter) == 0
        entry = await tickets.get_entry(session, contest_id, inviter)
        assert int(entry.tickets) == 1

    # Друг выполняет действие — реферал зачитывается, пригласивший получает билеты.
    async with session_scope() as session:
        await tickets.redeem_code_word(session, contest_id=contest_id, user_id=referred, text="CODE")
        await crefs.mark_conditions_met(session, contest_id=contest_id, user_id=referred)
        assert await crefs.credit_referral_for_user(session, contest_id=contest_id, user_id=referred) is True

    async with session_scope() as session:
        assert await crefs.count_referrals_for(session, contest_id, inviter) == 1
        entry = await tickets.get_entry(session, contest_id, inviter)
        assert int(entry.tickets) == 6  # 1 базовый + 5 за реферала
        # Повторный зачёт невозможен.
        assert await crefs.credit_referral_for_user(session, contest_id=contest_id, user_id=referred) is False


@pytest.mark.asyncio
async def test_referral_credited_immediately_without_conditions():
    contest_id = await _make_contest(tickets_per_ref=3)
    inviter, referred = 2101, 2102
    await _player(inviter)
    await _player(referred)
    async with session_scope() as session:
        session.add(Referral(inviter_id=inviter, invited_id=referred))
        await session.flush()

    bot = FakeBot()
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=inviter, first_name="Inv")
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=referred, first_name="Ref")

    async with session_scope() as session:
        assert await crefs.count_referrals_for(session, contest_id, inviter) == 1
        entry = await tickets.get_entry(session, contest_id, inviter)
        assert int(entry.tickets) == 4  # 1 базовый + 3 за реферала
        assert await lb.get_invited_count(session, contest_id, inviter) == 1


# ===================== РЕЙТИНГ И ИСТОЧНИКИ =====================

@pytest.mark.asyncio
async def test_leaderboard_and_sources_report():
    contest_id = await _make_contest(tickets_per_ref=2, leaderboard=True, leaderboard_size=10)
    inviter = 2201
    await _player(inviter)
    friends = [2202, 2203, 2204]
    bot = FakeBot()
    async with session_scope() as session:
        await cs.join_contest(session, bot, contest_id=contest_id, user_id=inviter, first_name="Inv")
    for f in friends:
        await _player(f)
        async with session_scope() as session:
            session.add(Referral(inviter_id=inviter, invited_id=f))
            await session.flush()
        async with session_scope() as session:
            await cs.join_contest(session, bot, contest_id=contest_id, user_id=f, first_name=f"F{f}")

    async with session_scope() as session:
        top = await lb.get_top(session, contest_id, limit=10)
    assert top and top[0][0] == inviter and top[0][1] == 3

    async with session_scope() as session:
        report = await cs.sources_report(session, contest_id)
    assert report and report[0]["invited"] == 3
    assert report[0]["tickets_awarded"] == 6  # 3 друга × 2 билета


# ===================== РОЗЫГРЫШ: ПРОЗРАЧНОСТЬ И ЗАПАСНЫЕ =====================

@pytest.mark.asyncio
async def test_draw_transparency_and_reserves():
    contest_id = await _make_contest(reserve_winners=2)
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        contest.winners_count = 2
        await session.flush()
    bot = FakeBot()
    for uid in range(3001, 3009):
        async with session_scope() as session:
            await cs.join_contest(session, bot, contest_id=contest_id, user_id=uid, first_name=f"U{uid}")

    async with session_scope() as session:
        result = await ds.run_draw(session, contest_id=contest_id, executed_by=1)
        assert len(result.winners) == 2
        assert len(result.reserves) == 2
        assert result.salt and result.participants_hash

    async with session_scope() as session:
        logs = await ds.get_draw_history(session, contest_id)
        assert logs
        log = logs[0]
        # Хэш воспроизводится из сохранённых данных и соли.
        recomputed = ds.compute_participants_hash(
            [int(x) for x in log.participants],
            {int(k): int(v) for k, v in log.weights.items()},
            log.salt,
        )
        assert recomputed == log.participants_hash
        # Розыгрыш детерминированно воспроизводится по seed.
        assert ds.verify_draw(log) == [int(x) for x in log.winners]

    async with session_scope() as session:
        reserves = await ds.get_latest_reserves(session, contest_id)
        assert len(reserves) == 2


@pytest.mark.asyncio
async def test_ticket_weights_influence_draw():
    """Участник с большим числом билетов получает пропорционально больший вес."""
    contest_id = await _make_contest(code_words=True, code_words_list=[{"word": "BIG", "tickets": 100}])
    bot = FakeBot()
    for uid in range(4001, 4004):
        async with session_scope() as session:
            await cs.join_contest(session, bot, contest_id=contest_id, user_id=uid, first_name=f"U{uid}")
    async with session_scope() as session:
        await tickets.redeem_code_word(session, contest_id=contest_id, user_id=4001, text="BIG")
    async with session_scope() as session:
        result = await ds.run_draw(session, contest_id=contest_id)
    # Вес = базовый вес × билеты.
    assert result.weights[4001] == ds.BASE_WEIGHT * 101
    assert result.weights[4002] == ds.BASE_WEIGHT


@pytest.mark.asyncio
async def test_reserve_transfer_on_timeout():
    contest_id = await _make_contest(reserve_winners=1, reserve_timeout_hours=24)
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        contest.winners_count = 1
        await session.flush()
    bot = FakeBot()
    for uid in range(5001, 5005):
        async with session_scope() as session:
            await cs.join_contest(session, bot, contest_id=contest_id, user_id=uid, first_name=f"U{uid}")
    async with session_scope() as session:
        result = await ds.run_draw(session, contest_id=contest_id)
        assert len(result.winners) == 1 and len(result.reserves) == 1
        main_id = result.winners[0]
        reserve_id = result.reserves[0]

    # Состариваем уведомление основного победителя, чтобы сработал таймаут.
    async with session_scope() as session:
        pending = await ds.get_pending_main_winners(session, contest_id)
        assert len(pending) == 1
        pending[0].notified_at = datetime.now(timezone.utc) - timedelta(hours=25)
        await session.flush()

    from app.tasks.background import _process_reserve_timeouts

    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        transferred = await _process_reserve_timeouts(session, bot, contest)
        assert transferred == 1

    async with session_scope() as session:
        winners = await ds.get_latest_winners(session, contest_id)
        winner_ids = [w.user_id for w, _ in winners]
        assert reserve_id in winner_ids
        assert main_id not in winner_ids
