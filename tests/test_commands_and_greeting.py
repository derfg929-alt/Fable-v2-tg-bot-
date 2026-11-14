"""Тесты для приветствия при старте/добавлении в чат и команды «команды»."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone
import pytest
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.types import Chat, User, Message, ChatMemberUpdated, ChatMemberAdministrator, Update

from app.services.help_service import get_greeting_text, get_commands_help_text
from app.handlers.social_moderation_handlers import handle_commands_list
from app.handlers.extended_moderation_handlers import (
    on_bot_added_my_chat_member,
    on_bot_added_new_chat_members,
)
from app.main import create_dispatcher
from app.db.session import session_scope
from app.services.chat_settings_service import get_or_create_chat


@pytest.mark.asyncio
async def test_help_service_content():
    """Проверка содержимого текстов приветствия и списка команд."""
    greeting = get_greeting_text()
    assert "Fable" in greeting
    assert "команды" in greeting.lower()
    assert "что я могу" in greeting.lower()

    commands = get_commands_help_text()
    assert "профиль" in commands
    assert "банк" in commands
    assert "биржа" in commands
    assert "рыбалка" in commands
    assert "дуэль" in commands
    assert "клан" in commands
    assert "топ" in commands
    assert "правила" in commands
    assert "настройки" in commands


@pytest.mark.asyncio
async def test_handle_commands_list():
    """Тест вызова обработчика 'команды'."""
    message = MagicMock(spec=Message)
    message.reply = AsyncMock()

    await handle_commands_list(message)

    message.reply.assert_called_once()
    args, kwargs = message.reply.call_args
    assert get_commands_help_text() in args[0]
    assert kwargs.get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_start_command_dispatcher():
    """Тест диспетчера при отправке /start."""
    dp = create_dispatcher()
    bot = AsyncMock()

    now = datetime.now(timezone.utc)
    user = User(id=12345, is_bot=False, first_name="Tester")
    chat = Chat(id=12345, type=ChatType.PRIVATE)
    message = Message(
        message_id=1,
        date=now,
        chat=chat,
        from_user=user,
        text="/start",
    )
    update = Update(update_id=100, message=message)

    # Подаем валидированный Update в диспетчер
    message = message.as_(bot)
    update = Update(update_id=100, message=message)
    await dp.feed_update(bot=bot, update=update)

    # В aiogram 3 message.answer(...) вызывает bot(SendMessage(...))
    call_args_list = bot.call_args_list
    assert len(call_args_list) > 0
    # Первый запуск в Fable Randomizer приветствует нового пользователя.
    send_msg = call_args_list[0][0][0]
    assert "Fable Randomizer" in send_msg.text
    assert "конкурсах" in send_msg.text.lower()


@pytest.mark.asyncio
async def test_on_bot_added_my_chat_member():
    """Тест события добавления бота в чат через my_chat_member."""
    bot = AsyncMock()
    chat_id = -100444555
    event = MagicMock(spec=ChatMemberUpdated)
    event.chat = Chat(id=chat_id, title="Test Group", type=ChatType.SUPERGROUP)
    event.new_chat_member = MagicMock(spec=ChatMemberAdministrator)
    event.new_chat_member.status = ChatMemberStatus.ADMINISTRATOR

    await on_bot_added_my_chat_member(event, bot)

    bot.send_message.assert_called_once()
    args, kwargs = bot.send_message.call_args
    assert kwargs.get("chat_id") == chat_id
    assert get_greeting_text() in kwargs.get("text")

    # Проверяем, что чат создан в БД
    async with session_scope() as session:
        c = await get_or_create_chat(session, chat_id=chat_id)
        assert c.title == "Test Group"


@pytest.mark.asyncio
async def test_on_bot_added_new_chat_members():
    """Тест события сообщения добавления бота через message.new_chat_members."""
    bot = AsyncMock()
    bot_user = User(id=999888, is_bot=True, first_name="FableBot")
    bot.get_me.return_value = bot_user

    chat_id = -100777888
    message = MagicMock(spec=Message)
    message.chat = Chat(id=chat_id, title="Another Test Group", type=ChatType.GROUP)
    message.new_chat_members = [
        User(id=111, is_bot=False, first_name="Alice"),
        bot_user,
    ]
    message.answer = AsyncMock()

    await on_bot_added_new_chat_members(message, bot)

    message.answer.assert_called_once()
    args, kwargs = message.answer.call_args
    assert get_greeting_text() in args[0]

    # Проверяем, что чат создан в БД
    async with session_scope() as session:
        c = await get_or_create_chat(session, chat_id=chat_id)
        assert c.title == "Another Test Group"
