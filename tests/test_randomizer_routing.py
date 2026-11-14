"""Тесты маршрутизации Fable Randomizer через диспетчер:
все кнопки главного меню имеют работающие обработчики, админ-панель доступна
только администраторам, а Mini App запускается из кнопки.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.methods import SendMessage

from app.db.session import session_scope
from app.services import randomizer_settings as rset
from app.services import contest_service as cs
from app.services.player_service import get_or_create_player


def _make_update(bot, user_id: int, text: str, update_id: int):
    now = datetime.now(timezone.utc)
    user = types.User(id=user_id, is_bot=False, first_name=f"U{user_id}")
    chat = types.Chat(id=user_id, type="private")
    msg = types.Message(message_id=1, date=now, chat=chat, from_user=user, text=text)
    msg = msg.as_(bot)
    return types.Update(update_id=update_id, message=msg)


def _sent_messages(bot):
    """Возвращает список SendMessage, которые бот отправил через __call__."""
    return [c[0][0] for c in bot.call_args_list if c and c[0] and isinstance(c[0][0], SendMessage)]


MENU_BUTTONS = [
    "🎁 Конкурсы",
    "📣 Мои конкурсы",
    "➕ Создать конкурс",
    "👤 Мой профиль",
    "👥 Пригласить друзей",
    "🏆 Победители",
    "📜 История участия",
    "ℹ️ Помощь",
]


@pytest.mark.asyncio
async def test_all_main_menu_buttons_have_handlers():
    from app.main import create_dispatcher

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    for i, label in enumerate(MENU_BUTTONS):
        bot.call_args_list.clear()
        await dp.feed_update(bot=bot, update=_make_update(bot, 840000 + i, label, update_id=100 + i))
        msgs = _sent_messages(bot)
        assert msgs, f"Нет ответа на «{label}»"
        assert msgs[0].text, f"Пустой ответ на «{label}»"


@pytest.mark.asyncio
async def test_admin_panel_access_control():
    from app.main import create_dispatcher

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    # Обычный пользователь не получает панель.
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_make_update(bot, 850000, "админ", update_id=200))
    msgs = _sent_messages(bot)
    assert msgs
    assert "АДМИН-ПАНЕЛЬ" not in msgs[0].text
    assert "админ" in msgs[0].text.lower() or "доступ" in msgs[0].text.lower()

    # Администратор получает панель со статистикой.
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=850001, first_name="Admin")
        await rset.add_admin(session, 850001, added_by=850001)

    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_make_update(bot, 850001, "админ", update_id=201))
    msgs = _sent_messages(bot)
    assert msgs
    assert "АДМИН-ПАНЕЛЬ" in msgs[0].text


@pytest.mark.asyncio
async def test_mini_app_button_present_when_configured(monkeypatch):
    """Если задан MINI_APP_URL, в /start появляется кнопка открытия Mini App."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "mini_app_url", "https://example.com/miniapp", raising=False)

    from app.main import create_dispatcher

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    await dp.feed_update(bot=bot, update=_make_update(bot, 860000, "/start", update_id=300))
    msgs = _sent_messages(bot)
    assert msgs
    kb = msgs[0].reply_markup
    assert kb is not None
    has_webapp = any(
        getattr(btn, "web_app", None) is not None
        for row in kb.keyboard for btn in row
    )
    assert has_webapp


def _callback_update(bot, user_id: int, data: str, update_id: int):
    now = datetime.now(timezone.utc)
    user = types.User(id=user_id, is_bot=False, first_name=f"U{user_id}")
    chat = types.Chat(id=user_id, type="private")
    msg = types.Message(message_id=5, date=now, chat=chat, from_user=types.User(id=999, is_bot=True, first_name="Bot"), text="panel")
    cb = types.CallbackQuery(id=f"cb{update_id}", from_user=user, chat_instance="ci", data=data, message=msg)
    return types.Update(update_id=update_id, callback_query=cb.as_(bot))


@pytest.mark.asyncio
async def test_admin_create_contest_wizard_end_to_end():
    """Полный цикл создания конкурса администратором через FSM-визард."""
    from app.main import create_dispatcher
    from app.db.models import Contest
    from sqlalchemy import select

    admin_id = 870001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        await rset.add_admin(session, admin_id, added_by=admin_id)

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    # 1. Нажимаем «Создать конкурс».
    await dp.feed_update(bot=bot, update=_callback_update(bot, admin_id, "adm_create", 400))

    # 2. Проходим шаги визарда.
    steps = [
        "Тестовый конкурс",   # title
        "Описание приза",     # description
        "-",                  # image (skip)
        "iPhone 16 Pro",      # prize
        "2",                  # winners
        "-",                  # starts (skip)
        "-",                  # ends (skip)
        "-",                  # channels (skip)
        "Быть подписанным",   # conditions
        "Текст поста",        # post
    ]
    for i, value in enumerate(steps):
        await dp.feed_update(bot=bot, update=_make_update(bot, admin_id, value, update_id=500 + i))

    # 3. Публикуем.
    await dp.feed_update(bot=bot, update=_callback_update(bot, admin_id, "adm_wiz_publish", 600))

    async with session_scope() as session:
        contest = await session.scalar(select(Contest).where(Contest.title == "Тестовый конкурс"))
        assert contest is not None
        assert contest.winners_count == 2
        assert contest.prize == "iPhone 16 Pro"
        assert contest.status == cs.STATUS_ACTIVE  # без даты старта публикуется активным


def _member(bot, status):
    async def _get_chat_member(chat_id, user_id):
        m = MagicMock()
        m.status = status
        m.is_member = status in ("member", "administrator", "creator")
        return m
    bot.get_chat_member = AsyncMock(side_effect=_get_chat_member)


@pytest.mark.asyncio
async def test_user_join_flow_via_callbacks():
    """Участие в конкурсе: проверка подписки, успех и защита от дублей."""
    from app.main import create_dispatcher
    from app.db.models import Channel

    async with session_scope() as session:
        contest = await cs.create_contest(session, title="JoinFlow", status=cs.STATUS_ACTIVE)
        cid = contest.id
        ch = Channel(chat_id=-100777000111, username="mustsub", title="Must", is_active=True)
        session.add(ch)
        await session.flush()
        await cs.set_contest_channels(session, cid, [ch.id])

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))
    user_id = 880001

    # Не подписан — получаем отказ с текстом про подписку.
    _member(bot, "left")
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_callback_update(bot, user_id, f"rz_join_{cid}", 700))
    texts = [m.text for m in _sent_messages(bot) if m.text]
    assert any("подписаться" in t.lower() for t in texts), texts

    # Подписался — участие успешно.
    _member(bot, "member")
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_callback_update(bot, user_id, f"rz_join_{cid}", 701))
    texts = [m.text for m in _sent_messages(bot) if m.text]
    assert any("успешно зарегистрирован" in t.lower() for t in texts), texts

    # Повторное участие запрещено.
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_callback_update(bot, user_id, f"rz_join_{cid}", 702))
    texts = [m.text for m in _sent_messages(bot) if m.text]
    assert any("уже участвуешь" in t.lower() for t in texts), texts

    async with session_scope() as session:
        assert await cs.participants_count(session, cid) == 1


@pytest.mark.asyncio
async def test_subscribe_required_dm_notification_in_group():
    """В группе при невыполненной подписке игроку уходит напоминание в ЛС."""
    from app.main import create_dispatcher
    from app.db.models import Channel

    async with session_scope() as session:
        contest = await cs.create_contest(session, title="SubDM", status=cs.STATUS_ACTIVE)
        cid = contest.id
        ch = Channel(chat_id=-100888000222, username="dmch", title="DM", is_active=True)
        session.add(ch)
        await session.flush()
        await cs.set_contest_channels(session, cid, [ch.id])

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))
    _member(bot, "left")

    now = datetime.now(timezone.utc)
    user = types.User(id=890001, is_bot=False, first_name="G")
    group = types.Chat(id=-100555000999, type="supergroup")
    msg = types.Message(message_id=1, date=now, chat=group, from_user=user, text=f"/join {cid}")
    update = types.Update(update_id=800, message=msg.as_(bot))
    await dp.feed_update(bot=bot, update=update)

    # Напоминание отправлено в ЛС игроку (send_message с chat_id=user_id).
    dm_chats = [
        c[1]["chat_id"] if "chat_id" in c[1] else c[0][0].chat_id
        for c in bot.send_message.call_args_list
    ]
    assert 890001 in dm_chats, dm_chats


@pytest.mark.asyncio
async def test_admin_draw_verify_and_redraw_callbacks():
    """Админские кнопки розыгрыша: проверка честности и повторный розыгрыш работают."""
    from app.main import create_dispatcher
    from app.db.models import ContestEntry
    from app.services import draw_service as ds

    admin_id = 875001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        await rset.add_admin(session, admin_id, added_by=admin_id)
        contest = await cs.create_contest(session, title="DrawBtn", status=cs.STATUS_ACTIVE, winners_count=1)
        cid = contest.id
        for i in range(3):
            await get_or_create_player(session, telegram_id=875100 + i, first_name=f"D{i}")
            session.add(ContestEntry(contest_id=cid, user_id=875100 + i, status="active"))

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    # Провести розыгрыш.
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_callback_update(bot, admin_id, f"adm_draw_{cid}", 900))
    async with session_scope() as session:
        assert await cs.winners_count(session, cid) == 1

    # Проверка честности — бот отвечает без ошибок.
    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_callback_update(bot, admin_id, f"adm_verify_{cid}", 901))
    assert bot.call_args_list, "нет ответа на проверку честности"

    # Меню повторного розыгрыша и режим с исключением прошлых победителей.
    await dp.feed_update(bot=bot, update=_callback_update(bot, admin_id, f"adm_redraw_{cid}", 902))
    await dp.feed_update(bot=bot, update=_callback_update(bot, admin_id, f"adm_redrawnew_{cid}", 903))
    async with session_scope() as session:
        logs = await ds.get_draw_history(session, cid)
        assert len(logs) == 2  # первый и повторный
        assert logs[0].round == 2


@pytest.mark.asyncio
async def test_all_notification_types_are_emitted_somewhere():
    """Каждый определённый тип уведомлений действительно используется в коде."""
    from pathlib import Path

    from app.services.randomizer_settings import NOTIFICATION_TYPES

    root = Path(__file__).resolve().parents[1]
    blob = ""
    for sub in ("app/handlers", "app/services", "app/tasks"):
        for path in (root / sub).rglob("*.py"):
            blob += path.read_text(encoding="utf-8")
    missing = [t for t in NOTIFICATION_TYPES if f'"{t}"' not in blob]
    assert not missing, f"Типы уведомлений без отправки: {missing}"
