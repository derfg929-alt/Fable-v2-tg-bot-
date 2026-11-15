"""Тесты серверного API Fable Randomizer (Mini App).

Проверяем, что API доверяет только проверенному Telegram initData, отдаёт
реальные данные из БД и корректно обрабатывает участие/отмену участия.
Эндпойнты вызываются напрямую (как корутины), что покрывает реальные ветки
валидации и работы с базой.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from app.api import (
    me,
    list_contests,
    contest_detail,
    join_contest,
    leave_contest,
    history,
    winners,
    JoinRequest,
    CodeWordRequest,
    submit_code_word,
    contest_participants,
    contest_leaderboard,
    contest_verify,
)
from app.config import get_settings
from app.db.session import session_scope
from app.db.models import Channel, Referral
from app.services import contest_service as cs
from app.services import draw_service as ds


def _init_data(user_id: int, *, token: str | None = None) -> str:
    token = token or get_settings().bot_token_value
    user = {"id": user_id, "first_name": f"User{user_id}", "username": f"user{user_id}"}
    pairs = {
        "user": json.dumps(user, separators=(",", ":")),
        "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
        "query_id": "TEST",
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


@pytest.mark.asyncio
async def test_me_requires_valid_init_data():
    # Без заголовка — 401.
    with pytest.raises(HTTPException) as exc:
        await me(x_init_data=None)
    assert exc.value.status_code == 401

    # С подделанным initData — 401.
    with pytest.raises(HTTPException):
        await me(x_init_data=_init_data(900001, token="999:wrong"))


@pytest.mark.asyncio
async def test_me_returns_real_data():
    data = await me(x_init_data=_init_data(900010))
    assert data["user"]["id"] == 900010
    assert "stats" in data and "invited" in data


@pytest.mark.asyncio
async def test_contests_and_detail_endpoints():
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="API Contest", status=cs.STATUS_ACTIVE, prize="iPhone")
        cid = contest.id

    listing = await list_contests(category="active", x_init_data=_init_data(900020))
    assert any(c["id"] == cid for c in listing["contests"])

    detail = await contest_detail(cid, x_init_data=_init_data(900020))
    assert detail["title"] == "API Contest"
    assert detail["is_participant"] is False


@pytest.mark.asyncio
async def test_api_join_and_leave(monkeypatch):
    # Бот в API создаётся внутри join_contest — подменяем aiogram.Bot, чтобы не ходить в сеть.
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id, user_id):
            m = MagicMock()
            m.status = "member"
            m.is_member = True
            return m

    monkeypatch.setattr(aiogram, "Bot", FakeBot)

    async with session_scope() as session:
        contest = await cs.create_contest(session, title="API Join", status=cs.STATUS_ACTIVE, allow_leave=True)
        cid = contest.id

    init = _init_data(900030)
    res = await join_contest(JoinRequest(contest_id=cid), x_init_data=init)
    assert res.ok is True

    # Повторное участие — уже участвуешь.
    res2 = await join_contest(JoinRequest(contest_id=cid), x_init_data=init)
    assert res2.body is not None  # JSONResponse для «уже участвуешь»
    assert json.loads(res2.body)["ok"] is False

    res3 = await leave_contest(JoinRequest(contest_id=cid), x_init_data=init)
    assert res3.ok is True


@pytest.mark.asyncio
async def test_api_join_not_subscribed(monkeypatch):
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id, user_id):
            m = MagicMock()
            m.status = "left"
            m.is_member = False
            return m

    monkeypatch.setattr(aiogram, "Bot", FakeBot)

    async with session_scope() as session:
        contest = await cs.create_contest(session, title="API Sub", status=cs.STATUS_ACTIVE)
        cid = contest.id
        ch = Channel(chat_id=-100555000111, username="subch", title="Sub", is_active=True)
        session.add(ch)
        await session.flush()
        await cs.set_contest_channels(session, cid, [ch.id])

    res = await join_contest(JoinRequest(contest_id=cid), x_init_data=_init_data(900040))
    assert res.body is not None
    payload = json.loads(res.body)
    assert payload["ok"] is False
    assert payload["reason"] == "not_subscribed"


@pytest.mark.asyncio
async def test_api_join_missing_contest(monkeypatch):
    # Участие в несуществующем конкурсе должно давать понятный ответ,
    # а не 500 из-за необработанного ContestNotFoundError.
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id, user_id):
            m = MagicMock()
            m.status = "member"
            m.is_member = True
            return m

    monkeypatch.setattr(aiogram, "Bot", FakeBot)

    res = await join_contest(JoinRequest(contest_id=9_999_999), x_init_data=_init_data(900070))
    assert res.status_code == 404
    payload = json.loads(res.body)
    assert payload["ok"] is False
    assert payload["reason"] == "not_found"


@pytest.mark.asyncio
async def test_api_join_huge_contest_id(monkeypatch):
    # Гигантский contest_id не помещается в целочисленный столбец БД; драйвер
    # бросает OverflowError, который должен превращаться в понятный 404, а не 500.
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id, user_id):
            m = MagicMock()
            m.status = "member"
            m.is_member = True
            return m

    monkeypatch.setattr(aiogram, "Bot", FakeBot)

    res = await join_contest(JoinRequest(contest_id=10**21), x_init_data=_init_data(900071))
    assert res.status_code == 404
    assert json.loads(res.body)["reason"] == "not_found"


@pytest.mark.asyncio
async def test_api_numeric_overflow_handler():
    # Глобальный обработчик превращает OverflowError/StatementError (нечисловой
    # по размеру id из URL) в 404 вместо 500 на всех эндпойнтах.
    from app.api import app

    handler = app.exception_handlers[OverflowError]
    resp = await handler(None, OverflowError("Python int too large to convert to SQLite INTEGER"))
    assert resp.status_code == 404
    assert json.loads(resp.body)["reason"] == "not_found"


@pytest.mark.asyncio
async def test_api_history_and_winners():
    init = _init_data(900050)
    h = await history(filter="all", x_init_data=init)
    assert "history" in h
    w = await winners(x_init_data=init)
    assert "contests" in w


@pytest.mark.asyncio
async def test_api_contest_detail_exposes_settings_and_tickets(monkeypatch):
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id, user_id):
            m = MagicMock()
            m.status = "member"
            m.is_member = True
            return m

    monkeypatch.setattr(aiogram, "Bot", FakeBot)

    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="Detail", status=cs.STATUS_ACTIVE,
            settings={"tickets_per_ref": 7, "code_words": True,
                      "code_words_list": [{"word": "W", "tickets": 2}]},
        )
        cid = contest.id

    await join_contest(JoinRequest(contest_id=cid), x_init_data=_init_data(900060))
    detail = await contest_detail(cid, x_init_data=_init_data(900060))
    assert detail["settings"]["tickets_per_ref"] == 7
    assert detail["is_participant"] is True
    assert detail["my_tickets"] == 1


@pytest.mark.asyncio
async def test_api_code_word_and_participants(monkeypatch):
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id, user_id):
            m = MagicMock()
            m.status = "member"
            m.is_member = True
            return m

    monkeypatch.setattr(aiogram, "Bot", FakeBot)

    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="CW", status=cs.STATUS_ACTIVE,
            settings={"code_words": True, "code_words_list": [{"word": "FABLE", "tickets": 3}]},
        )
        cid = contest.id

    init = _init_data(900070)
    await join_contest(JoinRequest(contest_id=cid), x_init_data=init)
    res = await submit_code_word(CodeWordRequest(contest_id=cid, word="fable"), x_init_data=init)
    assert res["ok"] is True
    # Повтор — уже введено.
    res2 = await submit_code_word(CodeWordRequest(contest_id=cid, word="FABLE"), x_init_data=init)
    assert res2["status"] == "already"

    parts = await contest_participants(cid, page=1, q=None, x_init_data=init)
    assert parts["total"] >= 1
    assert any(p["name"] for p in parts["participants"])


@pytest.mark.asyncio
async def test_api_leaderboard_and_verify(monkeypatch):
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id, user_id):
            m = MagicMock()
            m.status = "member"
            m.is_member = True
            return m

    monkeypatch.setattr(aiogram, "Bot", FakeBot)

    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="LB", status=cs.STATUS_ACTIVE, winners_count=1,
            settings={"leaderboard": True, "leaderboard_size": 5, "tickets_per_ref": 1},
        )
        cid = contest.id
        session.add(Referral(inviter_id=900081, invited_id=900082))
        await session.flush()

    for uid in (900081, 900082):
        await join_contest(JoinRequest(contest_id=cid), x_init_data=_init_data(uid))

    lb_data = await contest_leaderboard(cid, x_init_data=_init_data(900081))
    assert lb_data["leaderboard"][0]["invited"] == 1

    async with session_scope() as session:
        await ds.run_draw(session, contest_id=cid)
    ver = await contest_verify(cid, x_init_data=_init_data(900081))
    assert ver["rounds"] and ver["rounds"][0]["salt"]
    assert ver["rounds"][0]["participants_hash"]
