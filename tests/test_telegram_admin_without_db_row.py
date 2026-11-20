"""Тесты «админ Telegram без записи в БД»: права должны подтягиваться из Telegram.

Раньше проверка прав в сервисах вызывалась без bot=..., поэтому живой
администратор группы (создатель/админ в Telegram, но ещё не в chat_admins)
считался обычным игроком: «заморозка», «слоумод», «правило добавить»,
«ссылки добавить» и принятие жалоб молча падали с PermissionDenied.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.session import session_scope
from app.services.chat_settings_service import (
    DEFAULT_CHAT_SETTINGS,
    add_chat_rule,
    get_or_create_chat,
    update_chat_setting,
)
from app.services.moderation_service import (
    add_link_whitelist,
    get_user_admin_level,
)

CHAT_ID = -100910001
CREATOR = 910001
ADMIN = 910002
MEMBER = 910003


def _bot_with_status(status: str):
    bot = MagicMock()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status=status))
    return bot


@pytest.mark.asyncio
async def test_creator_without_db_row_resolves_to_level_5():
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=CHAT_ID, title="No DB Row")
        assert await get_user_admin_level(session, chat_id=CHAT_ID, user_id=CREATOR) == 1
        lvl = await get_user_admin_level(
            session, chat_id=CHAT_ID, user_id=CREATOR, bot=_bot_with_status("creator")
        )
        assert lvl == 5


@pytest.mark.asyncio
async def test_telegram_admin_can_update_settings_without_db_row():
    """«заморозка»/«слоумод» вызывают update_chat_setting — он должен пройти."""
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=CHAT_ID, title="No DB Row")
        bot = _bot_with_status("administrator")

        await update_chat_setting(
            session,
            chat_id=CHAT_ID,
            admin_id=ADMIN,
            key="slowmode_seconds",
            value=30,
            min_level=3,
            bot=bot,
        )

        chat = await get_or_create_chat(session, chat_id=CHAT_ID)
        assert (chat.settings or {})["slowmode_seconds"] == 30


@pytest.mark.asyncio
async def test_telegram_admin_can_add_rule_without_db_row():
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=CHAT_ID, title="No DB Row")
        rule = await add_chat_rule(
            session,
            chat_id=CHAT_ID,
            admin_id=ADMIN,
            text="Не спамить",
            bot=_bot_with_status("administrator"),
        )
        assert rule.position == 1


@pytest.mark.asyncio
async def test_telegram_admin_can_manage_link_whitelist():
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=CHAT_ID, title="No DB Row")
        domain = await add_link_whitelist(
            session,
            chat_id=CHAT_ID,
            admin_id=CREATOR,
            domain="Example.COM",
            bot=_bot_with_status("creator"),
        )
        assert domain == "example.com"


@pytest.mark.asyncio
async def test_plain_member_still_denied():
    """Обычный участник не должен получить права из-за этих изменений."""
    from app.services.moderation_service import PermissionDeniedError

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=CHAT_ID, title="No DB Row")
        with pytest.raises(PermissionDeniedError):
            await update_chat_setting(
                session,
                chat_id=CHAT_ID,
                admin_id=MEMBER,
                key="slowmode_seconds",
                value=30,
                min_level=3,
                bot=_bot_with_status("member"),
            )


@pytest.mark.asyncio
async def test_middleware_uses_bot_for_admin_level(monkeypatch):
    """Мидлварь обязан передавать bot: иначе админ получал бы авто-мут/удаление."""
    import app.middlewares as mw_module

    seen = {}

    async def spy(session, *, chat_id, user_id, bot=None):
        seen["bot"] = bot
        return 5

    monkeypatch.setattr(mw_module, "get_user_admin_level", spy)

    chat_id = -100910002
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="MW")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["antiflood_pause_seconds"] = 30
        chat.settings = settings
        await session.flush()

    from aiogram import types

    msg = MagicMock(spec=types.Message)
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="MW")
    msg.from_user = MagicMock(id=ADMIN, is_bot=False, first_name="Админ", username="admin")
    msg.text = "привет"
    msg.caption = None
    msg.message_id = 1
    msg.bot = MagicMock()
    msg.bot.id = 999
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()

    async def handler(event, data):
        return "handled"

    mw = mw_module.MessageActivityAndSecurityMiddleware()
    assert await mw(handler, msg, {}) == "handled"
    assert seen["bot"] is msg.bot
    assert not msg.delete.called
