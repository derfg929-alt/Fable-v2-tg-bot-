"""Тесты пользовательских конкурсов Fable Randomizer.

Проверяют:
- создание конкурса владельцем/администратором канала (проверка прав через
  Telegram API), запрет для обычных пользователей;
- правило «владелец канала не может выиграть в своём конкурсе»;
- статистику конкурса (участники, конверсия, пиковые часы регистраций);
- маршрутизацию: кнопки «📣 Мои конкурсы» и «➕ Создать конкурс», полный цикл
  создания конкурса через FSM-визард;
- API: /api/my-contests и /api/contests/{id}/stats с проверкой доступа.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest
from aiogram import types
from fastapi import HTTPException

from app.db.session import session_scope
from app.db.models import ContestEntry, Player
from app.services import channel_service as chs
from app.services import contest_service as cs
from app.services import draw_service as ds
from app.services.player_service import get_or_create_player


# ===================== ХЕЛПЕРЫ =====================

def _chat(chat_id: int, title: str = "My Channel", username: str = "mychannel"):
    chat = MagicMock()
    chat.id = chat_id
    chat.title = title
    chat.username = username
    chat.invite_link = None
    return chat


def _member(status: str):
    m = MagicMock()
    m.status = status
    m.is_member = status in ("member", "administrator", "creator")
    return m


def _bot_with_channel(
    chat_id: int,
    *,
    owner_status: str = "creator",
    bot_status: str = "administrator",
    username: str = "mychannel",
):
    """Бот, у которого get_chat/get_chat_member отвечают предсказуемо."""
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(id=777, username="FableRandomBot"))
    bot.get_chat = AsyncMock(return_value=_chat(chat_id, username=username))

    async def _get_chat_member(chat_id=None, user_id=None, **kwargs):
        if user_id == 777:
            return _member(bot_status)
        return _member(owner_status)

    bot.get_chat_member = AsyncMock(side_effect=_get_chat_member)
    return bot


# ===================== ПРОВЕРКА ПРАВ КАНАЛА =====================

@pytest.mark.asyncio
async def test_is_channel_admin_roles():
    bot = _bot_with_channel(-100111000111, owner_status="creator")
    assert await chs.is_channel_admin(bot, -100111000111, 500001) is True

    bot2 = _bot_with_channel(-100111000111, owner_status="administrator")
    assert await chs.is_channel_admin(bot2, -100111000111, 500001) is True

    bot3 = _bot_with_channel(-100111000111, owner_status="member")
    assert await chs.is_channel_admin(bot3, -100111000111, 500001) is False


@pytest.mark.asyncio
async def test_ensure_channel_creates_and_reuses():
    bot = _bot_with_channel(-100222000222, username="chan2")
    async with session_scope() as session:
        ch = await chs.ensure_channel(session, bot, "@chan2", added_by=500002)
        assert ch is not None and ch.chat_id == -100222000222
        first_id = ch.id
    async with session_scope() as session:
        ch2 = await chs.ensure_channel(session, bot, "https://t.me/chan2", added_by=500002)
        assert ch2 is not None and ch2.id == first_id


def test_normalize_channel_token():
    assert chs.normalize_channel_token("@abc") == "@abc"
    assert chs.normalize_channel_token("abc") == "@abc"
    assert chs.normalize_channel_token("https://t.me/abc") == "@abc"
    assert chs.normalize_channel_token("t.me/abc/123") == "@abc"
    assert chs.normalize_channel_token("-100123") == "-100123"


# ===================== ВЛАДЕЛЕЦ НЕ МОЖЕТ ВЫИГРАТЬ =====================

@pytest.mark.asyncio
async def test_owner_cannot_win_own_contest():
    owner_id = 600001
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_member("member"))
    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="OwnerDraw", status=cs.STATUS_ACTIVE, winners_count=1, owner_id=owner_id
        )
        cid = contest.id
        # Владелец тоже «участвует», но не должен победить.
        await get_or_create_player(session, telegram_id=owner_id, first_name="Owner")
        session.add(ContestEntry(contest_id=cid, user_id=owner_id, status="active"))
        for i in range(5):
            await get_or_create_player(session, telegram_id=600100 + i, first_name=f"P{i}")
            session.add(ContestEntry(contest_id=cid, user_id=600100 + i, status="active"))

    # Несколько розыгрышей: владелец ни разу не должен попасть в победители.
    for round_no in range(5):
        async with session_scope() as session:
            result = await ds.run_draw(session, contest_id=cid, executed_by=1)
            assert owner_id not in result.winners, "владелец выиграл в своём конкурсе"
            assert owner_id not in result.participants
            assert len(result.participants) == 5


@pytest.mark.asyncio
async def test_owner_cannot_join_own_contest():
    owner_id = 605001
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_member("member"))
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="OwnerJoin", status=cs.STATUS_ACTIVE, owner_id=owner_id)
        cid = contest.id
    async with session_scope() as session:
        with pytest.raises(cs.OwnerCannotJoinError):
            await cs.join_contest(session, bot, contest_id=cid, user_id=owner_id, first_name="Owner")
    # Другой игрок спокойно участвует.
    async with session_scope() as session:
        entry = await cs.join_contest(session, bot, contest_id=cid, user_id=605002, first_name="Other")
        assert entry.status == "active"


@pytest.mark.asyncio
async def test_draw_without_owner_unchanged():
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_member("member"))
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="NoOwner", status=cs.STATUS_ACTIVE, winners_count=2)
        cid = contest.id
        for i in range(4):
            await get_or_create_player(session, telegram_id=610000 + i, first_name=f"Q{i}")
            session.add(ContestEntry(contest_id=cid, user_id=610000 + i, status="active"))
    async with session_scope() as session:
        result = await ds.run_draw(session, contest_id=cid)
        assert len(result.participants) == 4
        assert len(result.winners) == 2


# ===================== СТАТИСТИКА =====================

@pytest.mark.asyncio
async def test_contest_statistics_and_peak_hours():
    owner_id = 620001
    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="StatsContest", status=cs.STATUS_ACTIVE, owner_id=owner_id,
            starts_at=datetime.now(timezone.utc) - timedelta(days=2),
            ends_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        cid = contest.id
        # 3 регистрации в 15:00, 1 в 09:00.
        hours = [15, 15, 15, 9]
        for i, h in enumerate(hours):
            uid = 620100 + i
            await get_or_create_player(session, telegram_id=uid, first_name=f"S{i}")
            joined = datetime.now(timezone.utc).replace(hour=h, minute=0, second=0, microsecond=0)
            session.add(ContestEntry(contest_id=cid, user_id=uid, status="active", joined_at=joined))
        # Один отменил участие.
        session.add(ContestEntry(contest_id=cid, user_id=620999, status="left",
                                 joined_at=datetime.now(timezone.utc)))
        await get_or_create_player(session, telegram_id=620999, first_name="Left")

    async with session_scope() as session:
        stats = await cs.contest_statistics(session, cid)
        assert stats["participants_active"] == 4
        assert stats["participants_left"] == 1
        assert stats["participants_total"] == 5
        assert stats["timeline"]["peak_hour"] == 15
        assert stats["timeline"]["peak_hour_value"] == 3
        assert stats["timeline"]["by_hour"][15] == 3
        assert stats["timeline"]["by_hour"][9] == 1
        assert stats["duration_days"] is not None and stats["duration_days"] > 3


# ===================== МАРШРУТИЗАЦИЯ =====================

def _make_update(bot, user_id: int, text: str, update_id: int):
    now = datetime.now(timezone.utc)
    user = types.User(id=user_id, is_bot=False, first_name=f"U{user_id}")
    chat = types.Chat(id=user_id, type="private")
    msg = types.Message(message_id=1, date=now, chat=chat, from_user=user, text=text)
    return types.Update(update_id=update_id, message=msg.as_(bot))


def _callback_update(bot, user_id: int, data: str, update_id: int):
    now = datetime.now(timezone.utc)
    user = types.User(id=user_id, is_bot=False, first_name=f"U{user_id}")
    chat = types.Chat(id=user_id, type="private")
    msg = types.Message(message_id=5, date=now, chat=chat,
                        from_user=types.User(id=999, is_bot=True, first_name="Bot"), text="panel")
    cb = types.CallbackQuery(id=f"cb{update_id}", from_user=user, chat_instance="ci", data=data, message=msg)
    return types.Update(update_id=update_id, callback_query=cb.as_(bot))


def _sent_messages(bot):
    from aiogram.methods import SendMessage
    return [c[0][0] for c in bot.call_args_list if c and c[0] and isinstance(c[0][0], SendMessage)]


@pytest.mark.asyncio
async def test_my_contests_menu_button_has_handler():
    from app.main import create_dispatcher

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_make_update(bot, 630001, "📣 Мои конкурсы", update_id=1000))
    msgs = _sent_messages(bot)
    assert msgs, "нет ответа на «📣 Мои конкурсы»"


@pytest.mark.asyncio
async def test_user_contest_wizard_end_to_end():
    """Полный цикл: владелец канала создаёт конкурс для своего канала."""
    from app.main import create_dispatcher
    from sqlalchemy import select
    from app.db.models import Contest

    owner_id = 640001
    bot = _bot_with_channel(-100640640640, owner_status="creator")

    dp = create_dispatcher()
    await dp.feed_update(bot=bot, update=_make_update(bot, owner_id, "➕ Создать конкурс", update_id=1100))
    # Шаг 1 — канал (проверяются права владельца).
    await dp.feed_update(bot=bot, update=_make_update(bot, owner_id, "@mychannel", update_id=1101))
    steps = [
        "Конкурс канала",   # title
        "Описание",         # description
        "-",                # image
        "AirPods",          # prize
        "1",                # winners
        "-",                # ends
        "Подписка",         # conditions
    ]
    for i, value in enumerate(steps):
        await dp.feed_update(bot=bot, update=_make_update(bot, owner_id, value, update_id=1110 + i))
    # Публикуем.
    await dp.feed_update(bot=bot, update=_callback_update(bot, owner_id, "uc_wiz_publish", 1200))

    async with session_scope() as session:
        contest = await session.scalar(select(Contest).where(Contest.title == "Конкурс канала"))
        assert contest is not None
        assert contest.owner_id == owner_id
        assert contest.source_channel_id is not None
        assert contest.status == cs.STATUS_ACTIVE
        # Обязательная подписка на канал владельца добавлена.
        channels = await cs.get_contest_channels(session, contest.id)
        assert len(channels) == 1


@pytest.mark.asyncio
async def test_user_contest_wizard_rejects_non_admin():
    """Обычный подписчик не может создать конкурс."""
    from app.main import create_dispatcher
    from sqlalchemy import select
    from app.db.models import Contest

    user_id = 650001
    bot = _bot_with_channel(-100650650650, owner_status="member")

    dp = create_dispatcher()
    await dp.feed_update(bot=bot, update=_make_update(bot, user_id, "➕ Создать конкурс", update_id=1300))
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_make_update(bot, user_id, "@notmine", update_id=1301))
    texts = [m.text for m in _sent_messages(bot) if m.text]
    assert any("не являешься" in t or "администратор" in t.lower() for t in texts), texts

    async with session_scope() as session:
        contest = await session.scalar(select(Contest).where(Contest.owner_id == user_id))
        assert contest is None


@pytest.mark.asyncio
async def test_owner_sees_stats_and_draws_without_winning():
    from app.main import create_dispatcher
    from app.db.models import Winner
    from sqlalchemy import select

    owner_id = 660001
    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="OwnerManage", status=cs.STATUS_FINISHED, winners_count=1, owner_id=owner_id
        )
        cid = contest.id
        await get_or_create_player(session, telegram_id=owner_id, first_name="Owner")
        session.add(ContestEntry(contest_id=cid, user_id=owner_id, status="active"))
        for i in range(3):
            await get_or_create_player(session, telegram_id=660100 + i, first_name=f"W{i}")
            session.add(ContestEntry(contest_id=cid, user_id=660100 + i, status="active"))

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    # Статистика доступна владельцу.
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_callback_update(bot, owner_id, f"uc_stats_{cid}", 1400))
    assert bot.call_args_list

    # Розыгрыш владельцем — он сам не становится победителем.
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_callback_update(bot, owner_id, f"uc_draw_{cid}", 1401))
    async with session_scope() as session:
        winners = (await session.scalars(select(Winner.user_id).where(Winner.contest_id == cid))).all()
        assert winners and owner_id not in winners


# ===================== API =====================

def _init_data(user_id: int, *, token: str | None = None) -> str:
    from app.config import get_settings
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
async def test_api_my_contests_and_stats_access():
    from app.api import my_contests, contest_stats

    owner_id = 670001
    other_id = 670002
    async with session_scope() as session:
        contest = await cs.create_contest(
            session, title="APIOwner", status=cs.STATUS_ACTIVE, owner_id=owner_id
        )
        cid = contest.id

    mine = await my_contests(x_init_data=_init_data(owner_id))
    assert any(c["id"] == cid for c in mine["contests"])

    # Владелец получает статистику.
    stats = await contest_stats(cid, x_init_data=_init_data(owner_id))
    assert stats["contest_id"] == cid

    # Посторонний получает 403.
    with pytest.raises(HTTPException) as exc:
        await contest_stats(cid, x_init_data=_init_data(other_id))
    assert exc.value.status_code == 403

    # Несуществующий конкурс — 404.
    with pytest.raises(HTTPException) as exc2:
        await contest_stats(999999, x_init_data=_init_data(owner_id))
    assert exc2.value.status_code == 404


@pytest.mark.asyncio
async def test_api_contest_stats_admin_allowed():
    from app.api import contest_stats
    from app.services import randomizer_settings as rset

    admin_id = 680001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=admin_id, first_name="Adm")
        await rset.add_admin(session, admin_id, added_by=admin_id)
        contest = await cs.create_contest(session, title="AdminStats", status=cs.STATUS_ACTIVE, owner_id=999000)
        cid = contest.id

    stats = await contest_stats(cid, x_init_data=_init_data(admin_id))
    assert stats["contest_id"] == cid
