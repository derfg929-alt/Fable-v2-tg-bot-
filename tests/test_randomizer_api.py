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


# ===================== УПРАВЛЕНИЕ КОНКУРСОМ ВЛАДЕЛЬЦЕМ (Mini App) =====================

def _fake_bot(monkeypatch):
    import aiogram

    class FakeBot:
        def __init__(self, *a, **k):
            self.session = AsyncMock()

        async def get_chat_member(self, chat_id=None, user_id=None, **kwargs):
            m = MagicMock()
            m.status = "member"
            m.is_member = True
            return m

        async def send_message(self, *a, **k):
            return MagicMock()

    monkeypatch.setattr(aiogram, "Bot", FakeBot)


async def _owner_contest(owner_id: int, **kwargs):
    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="Owner Contest", status=cs.STATUS_ACTIVE,
            owner_id=owner_id, **kwargs,
        )
        return contest.id


@pytest.mark.asyncio
async def test_api_owner_update_and_settings(monkeypatch):
    from app.api import update_contest, update_contest_settings, ContestUpdateRequest, ContestSettingsRequest

    _fake_bot(monkeypatch)
    cid = await _owner_contest(910001)

    res = await update_contest(
        cid,
        ContestUpdateRequest(title="Новое имя", prize="Prize", winners_count=3),
        x_init_data=_init_data(910001),
    )
    assert res["ok"] and res["contest"]["title"] == "Новое имя"
    assert res["contest"]["winners_count"] == 3

    sres = await update_contest_settings(
        cid, ContestSettingsRequest(settings={"leaderboard": True, "tickets_per_ref": 5}),
        x_init_data=_init_data(910001),
    )
    assert sres["settings"]["leaderboard"] is True
    assert sres["settings"]["tickets_per_ref"] == 5


@pytest.mark.asyncio
async def test_api_owner_actions_require_owner(monkeypatch):
    from app.api import update_contest, ContestUpdateRequest

    _fake_bot(monkeypatch)
    cid = await _owner_contest(910010)
    with pytest.raises(HTTPException) as exc:
        await update_contest(cid, ContestUpdateRequest(title="X"), x_init_data=_init_data(910011))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_api_owner_finish_cancel_archive_delete(monkeypatch):
    from app.api import finish_contest, cancel_contest, archive_contest, delete_contest

    _fake_bot(monkeypatch)
    cid = await _owner_contest(910020)

    fin = await finish_contest(cid, x_init_data=_init_data(910020))
    assert fin["ok"] is True
    async with session_scope() as session:
        assert (await cs.get_contest(session, cid)).status == cs.STATUS_FINISHED

    cid2 = await _owner_contest(910020)
    canc = await cancel_contest(cid2, x_init_data=_init_data(910020))
    assert canc["ok"] is True
    async with session_scope() as session:
        assert (await cs.get_contest(session, cid2)).status == cs.STATUS_CANCELLED

    cid3 = await _owner_contest(910020)
    arch = await archive_contest(cid3, x_init_data=_init_data(910020))
    assert arch["ok"] is True
    async with session_scope() as session:
        assert (await cs.get_contest(session, cid3)).is_archived is True

    cid4 = await _owner_contest(910020)
    dele = await delete_contest(cid4, x_init_data=_init_data(910020))
    assert dele["ok"] is True
    async with session_scope() as session:
        assert (await cs.get_contest(session, cid4)) is None


@pytest.mark.asyncio
async def test_api_owner_draw_and_results(monkeypatch):
    from app.api import draw_contest, contest_results, join_contest, JoinRequest, DrawRequest

    _fake_bot(monkeypatch)
    cid = await _owner_contest(910030, winners_count=1)

    # Двое участников (не владелец).
    for uid in (910031, 910032):
        await join_contest(JoinRequest(contest_id=cid), x_init_data=_init_data(uid))

    res = await draw_contest(cid, DrawRequest(), x_init_data=_init_data(910030))
    assert res["ok"] is True
    assert len(res["winners"]) == 1

    results = await contest_results(cid, x_init_data=_init_data(910030))
    assert len(results["winners"]) == 1
    assert results["draws"][0]["round"] == 1


@pytest.mark.asyncio
async def test_api_draw_requires_owner(monkeypatch):
    from app.api import draw_contest, DrawRequest

    _fake_bot(monkeypatch)
    cid = await _owner_contest(910040)
    with pytest.raises(HTTPException) as exc:
        await draw_contest(cid, DrawRequest(), x_init_data=_init_data(910041))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_api_participants_search_finds_any_page(monkeypatch):
    """Поиск участников должен находить игрока не только на первой странице."""
    from app.api import contest_participants, join_contest, JoinRequest

    _fake_bot(monkeypatch)

    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="Search", status=cs.STATUS_ACTIVE,
            settings={"show_participants": True},
        )
        cid = contest.id

    # 60 участников: цель поиска — последняя (ушла бы на 2-ю страницу).
    target = 920060
    for uid in range(920001, 920061):
        await join_contest(JoinRequest(contest_id=cid), x_init_data=_init_data(uid))

    page1 = await contest_participants(cid, page=1, q=None, x_init_data=_init_data(920001))
    assert page1["total"] == 60
    assert len(page1["participants"]) == 50

    found = await contest_participants(cid, page=1, q=str(target), x_init_data=_init_data(920001))
    assert found["total"] == 1
    assert found["participants"][0]["name"] == f"User{target}"


@pytest.mark.asyncio
async def test_api_me_returns_activity_and_days_in_bot():
    """Профиль в Mini App должен показывать стаж и число сообщений.

    Без этих полей пользователь видел только дату регистрации и не понимал,
    сколько он реально написал — именно это и было в жалобе «не показывает,
    сколько я зареган и сколько сообщений отправил».
    """
    from datetime import timedelta

    from app.services.activity_service import record_message_activity
    from app.services.chat_settings_service import get_or_create_chat
    from app.services.player_service import get_or_create_player

    user_id = 771001
    chat_id = -100771001
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Activity")
        reg = await get_or_create_player(session, telegram_id=user_id, first_name="Аня")
        reg.player.registered_at = datetime.now(timezone.utc) - timedelta(days=30)
        for i in range(1, 6):
            await record_message_activity(
                session, chat_id=chat_id, user_id=user_id, message_id=i, text=f"сообщение {i}"
            )
        await session.flush()

    payload = await me(x_init_data=_init_data(user_id))

    assert payload["user"]["days_in_bot"] == 30
    assert payload["activity"]["messages_all_time"] == 5
    assert payload["activity"]["chats"] == 1
    assert payload["activity"]["last_active"] is not None
