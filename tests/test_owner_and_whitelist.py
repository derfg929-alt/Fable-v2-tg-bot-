"""Регрессии команд владельца и белого списка ссылок.

Раньше «выдать»/«забрать» падали с TypeError/AttributeError, а «ссылки
добавить/удалить» — с NameError, поэтому кнопки и команды не работали.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.methods import SendMessage

from app.config import get_settings
from app.db.models import ChatAdmin
from app.db.session import session_scope
from app.services.chat_settings_service import get_or_create_chat
from app.services.moderation_service import list_link_whitelist
from app.services.player_service import get_or_create_player, get_player

OWNER_ID = get_settings().owner_id
GROUP_ID = -1009876543210


def _make_update(bot, user_id: int, text: str, update_id: int, *, chat_id: int | None = None, chat_type: str = "private"):
    now = datetime.now(timezone.utc)
    user = types.User(id=user_id, is_bot=False, first_name=f"U{user_id}", username=f"u{user_id}")
    chat = types.Chat(id=chat_id or user_id, type=chat_type, title="G" if chat_type != "private" else None)
    msg = types.Message(message_id=update_id, date=now, chat=chat, from_user=user, text=text)
    return types.Update(update_id=update_id, message=msg.as_(bot))


def _sent_texts(bot) -> list[str]:
    texts: list[str] = []
    for call in bot.call_args_list:
        if call.args and isinstance(call.args[0], SendMessage):
            texts.append(call.args[0].text or "")
    return texts


def _bot_mock() -> AsyncMock:
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableBot"))
    member = MagicMock()
    member.status = "administrator"
    member.is_member = True
    member.can_post_messages = True
    member.can_manage_chat = True
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.get_chat = AsyncMock(return_value=MagicMock(id=GROUP_ID, title="G", username="g"))
    bot.get_chat_administrators = AsyncMock(return_value=[])
    return bot


@pytest.mark.asyncio
async def test_owner_can_give_and_take_coins():
    from app.main import create_dispatcher

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=OWNER_ID, first_name="Owner", username="owner")
        await get_or_create_player(session, telegram_id=600, first_name="Target", username="target")

    dp = create_dispatcher()
    bot = _bot_mock()

    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "выдать 600 100", 1))
    assert any("Выдано" in t and "баланс: <b>100</b>" in t for t in _sent_texts(bot))
    async with session_scope() as session:
        assert (await get_player(session, 600)).wallet == 100

    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "забрать 600 40", 2))
    assert any("Списано" in t and "60" in t for t in _sent_texts(bot))
    async with session_scope() as session:
        assert (await get_player(session, 600)).wallet == 60


@pytest.mark.asyncio
async def test_owner_give_creates_missing_player():
    from app.main import create_dispatcher

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=OWNER_ID, first_name="Owner", username="owner")

    dp = create_dispatcher()
    bot = _bot_mock()
    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "выдать 777 25", 3))

    assert any("Выдано" in t for t in _sent_texts(bot))
    async with session_scope() as session:
        player = await get_player(session, 777)
        assert player is not None and player.wallet == 25


@pytest.mark.asyncio
async def test_owner_take_missing_player_reports_error():
    from app.main import create_dispatcher

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=OWNER_ID, first_name="Owner", username="owner")

    dp = create_dispatcher()
    bot = _bot_mock()
    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "забрать 999 5", 4))

    assert any("не зарегистрирован" in t for t in _sent_texts(bot))


@pytest.mark.asyncio
async def test_whitelist_add_and_remove_work():
    from app.main import create_dispatcher

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=OWNER_ID, first_name="Owner", username="owner")
        await get_or_create_chat(session, chat_id=GROUP_ID, title="G")
        session.add(ChatAdmin(chat_id=GROUP_ID, user_id=OWNER_ID, level=5, is_active=True))
        await session.flush()

    dp = create_dispatcher()
    bot = _bot_mock()
    kwargs = {"chat_id": GROUP_ID, "chat_type": "supergroup"}

    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "ссылки добавить example.com", 5, **kwargs))
    assert any("добавлен" in t for t in _sent_texts(bot))
    async with session_scope() as session:
        assert "example.com" in await list_link_whitelist(session, chat_id=GROUP_ID)

    bot.call_args_list.clear()
    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "ссылки удалить example.com", 6, **kwargs))
    assert any("удален" in t for t in _sent_texts(bot))
    async with session_scope() as session:
        assert "example.com" not in await list_link_whitelist(session, chat_id=GROUP_ID)


@pytest.mark.asyncio
async def test_unknown_bank_action_and_freeze_duration_have_clear_replies():
    from app.main import create_dispatcher

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=OWNER_ID, first_name="Owner", username="owner")
        await get_or_create_chat(session, chat_id=GROUP_ID, title="G")
        session.add(ChatAdmin(chat_id=GROUP_ID, user_id=OWNER_ID, level=5, is_active=True))
        await session.flush()

    dp = create_dispatcher()
    bot = _bot_mock()

    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "банк вклад 100", 7))
    assert any("банк положить" in t for t in _sent_texts(bot))

    bot.call_args_list.clear()
    group = {"chat_id": GROUP_ID, "chat_type": "supergroup"}
    await dp.feed_update(bot=bot, update=_make_update(bot, OWNER_ID, "заморозка 10", 8, **group))
    assert any("10 мин." in t and "мин.." not in t for t in _sent_texts(bot))
