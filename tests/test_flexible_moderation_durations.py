"""Тесты для проверки гибких форматов времени модерации (секунды, минуты, часы, дни, сложные фразы) и inline-кнопок."""
from __future__ import annotations

from datetime import timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.db.session import session_scope
from app.db.models import Player, ChatAdmin, ModerationCase
from app.services.player_service import get_or_create_player
from app.services.chat_settings_service import get_or_create_chat
from app.services.moderation_service import (
    parse_duration_string,
    extract_moderation_args,
    issue_mute,
    record_ban_or_kick,
)
from app.handlers.social_moderation_handlers import handle_mute, handle_unmute, on_help_section_click, on_top_menu_rich
from app.handlers.extended_moderation_handlers import handle_ban, handle_unban, handle_freeze


def test_flexible_parse_duration_units():
    # Секунды
    assert parse_duration_string("67000 сек") == timedelta(seconds=67000)
    assert parse_duration_string("67000 секунд") == timedelta(seconds=67000)
    assert parse_duration_string("500с") == timedelta(seconds=500)
    assert parse_duration_string("100s") == timedelta(seconds=100)

    # Минуты
    assert parse_duration_string("30м") == timedelta(minutes=30)
    assert parse_duration_string("120 мин") == timedelta(minutes=120)
    assert parse_duration_string("45 минут") == timedelta(minutes=45)
    assert parse_duration_string("полчаса") == timedelta(minutes=30)

    # Часы и сложные сочетания
    assert parse_duration_string("1 час 30 минут") == timedelta(hours=1, minutes=30)
    assert parse_duration_string("час 30") == timedelta(hours=1, minutes=30)
    assert parse_duration_string("час 2") == timedelta(hours=2)
    assert parse_duration_string("на час-два") == timedelta(hours=2)
    assert parse_duration_string("на 2 часа") == timedelta(hours=2)
    assert parse_duration_string("на час") == timedelta(hours=1)
    assert parse_duration_string("1ч 30м") == timedelta(hours=1, minutes=30)
    assert parse_duration_string("1ч30м") == timedelta(hours=1, minutes=30)
    assert parse_duration_string("2ч") == timedelta(hours=2)

    # Дни
    assert parse_duration_string("1 день") == timedelta(days=1)
    assert parse_duration_string("3 дня") == timedelta(days=3)
    assert parse_duration_string("7д") == timedelta(days=7)
    assert parse_duration_string("1д") == timedelta(days=1)


def test_extract_moderation_args():
    # 'мут на 67000 сек спам'
    dur, disp, reason = extract_moderation_args("мут на 67000 сек спам")
    assert dur == timedelta(seconds=67000)
    assert disp == "67000 сек."
    assert reason == "спам"

    # 'мут 1 час 30 флуд'
    dur, disp, reason = extract_moderation_args("мут 1 час 30 флуд")
    assert dur == timedelta(hours=1, minutes=30)
    assert disp == "1 ч. 30 мин."
    assert reason == "флуд"

    # 'мут на час 2 спам'
    dur, disp, reason = extract_moderation_args("мут на час 2 спам")
    assert dur == timedelta(hours=2)
    assert disp == "2 ч."
    assert reason == "спам"

    # 'бан на 2 дня мат'
    dur, disp, reason = extract_moderation_args("бан на 2 дня мат", default_duration=None)
    assert dur == timedelta(days=2)
    assert disp == "2 д."
    assert reason == "мат"

    # 'бан читы' (по умолчанию бессрочно)
    dur, disp, reason = extract_moderation_args("бан читы", default_duration=None)
    assert dur is None
    assert disp == "навсегда"
    assert reason == "читы"


@pytest.mark.asyncio
async def test_mute_with_custom_seconds_and_hours():
    chat_id = -100888999111
    mod_id = 9001001
    target_id = 9002002

    mock_bot = MagicMock()
    mock_bot.restrict_chat_member = AsyncMock(return_value=True)

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Test Mute Chat")
        await get_or_create_player(session, telegram_id=mod_id, first_name="Moderator")
        await get_or_create_player(session, telegram_id=target_id, first_name="TargetUser")
        session.add(ChatAdmin(chat_id=chat_id, user_id=mod_id, level=3, assigned_by=mod_id, is_active=True))
        await session.flush()

    # 1. Мут на 67000 сек
    reply_msg = MagicMock()
    reply_msg.from_user = MagicMock(id=target_id, first_name="TargetUser", is_bot=False)

    mute_msg = MagicMock()
    mute_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Test Mute Chat")
    mute_msg.from_user = MagicMock(id=mod_id, first_name="Moderator", is_bot=False)
    mute_msg.reply_to_message = reply_msg
    mute_msg.text = "мут на 67000 сек спам в чате"
    mute_msg.bot = mock_bot
    mute_msg.reply = AsyncMock()

    await handle_mute(mute_msg)
    assert mute_msg.reply.called
    reply_text = mute_msg.reply.call_args[0][0]
    assert "ПОЛЬЗОВАТЕЛЬ ЗАМЬЮЧЕН!" in reply_text
    assert "67000 сек." in reply_text
    assert "спам в чате" in reply_text
    assert mock_bot.restrict_chat_member.called

    # 2. Мут на час 30
    mute_msg2 = MagicMock()
    mute_msg2.chat = MagicMock(id=chat_id, type="supergroup", title="Test Mute Chat")
    mute_msg2.from_user = MagicMock(id=mod_id, first_name="Moderator", is_bot=False)
    mute_msg2.reply_to_message = reply_msg
    mute_msg2.text = "мут 1 час 30 оффтоп"
    mute_msg2.bot = mock_bot
    mute_msg2.reply = AsyncMock()

    await handle_mute(mute_msg2)
    assert mute_msg2.reply.called
    reply_text2 = mute_msg2.reply.call_args[0][0]
    assert "ПОЛЬЗОВАТЕЛЬ ЗАМЬЮЧЕН!" in reply_text2
    assert "1 ч. 30 мин." in reply_text2
    assert "оффтоп" in reply_text2


@pytest.mark.asyncio
async def test_ban_with_flexible_time():
    chat_id = -100888999112
    admin_id = 9003001
    target_id = 9004002

    mock_bot = MagicMock()
    mock_bot.ban_chat_member = AsyncMock(return_value=True)

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Test Ban Chat")
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        await get_or_create_player(session, telegram_id=target_id, first_name="BannedUser")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=4, assigned_by=admin_id, is_active=True))
        await session.flush()

    reply_msg = MagicMock()
    reply_msg.from_user = MagicMock(id=target_id, first_name="BannedUser", is_bot=False)

    ban_msg = MagicMock()
    ban_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Test Ban Chat")
    ban_msg.from_user = MagicMock(id=admin_id, first_name="Admin", is_bot=False)
    ban_msg.reply_to_message = reply_msg
    ban_msg.text = "бан на 2 дня токсичность"
    ban_msg.reply = AsyncMock()

    await handle_ban(ban_msg, bot=mock_bot)
    assert ban_msg.reply.called
    reply_text = ban_msg.reply.call_args[0][0]
    assert "ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН (БАН)!" in reply_text
    assert "2 д." in reply_text
    assert "токсичность" in reply_text
    assert mock_bot.ban_chat_member.called
