"""Тесты для новых функций:
- Настройки Grok AI в Settings
- Триггеры 'грок'/'grok' и обработка диалогов в ЛС
- Безопасная отправка и разбивка длинных сообщений Grok
- Рандомизация интервалов рассылок (3-6 часов) и чатов в engagement worker
- Отображение подключенных чатов в админ-панели владельца
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram import types
from aiogram.enums import ChatType

from app.config import Settings
from app.db.session import session_scope
from app.db.models import Chat
from app.handlers.ai_handlers import (
    _should_trigger_ai,
    _split_into_chunks,
    _send_chunk,
    _send_ai_response,
    handle_ai_query,
)
from app.services.engagement_service import process_chat_baits
from app.tasks.background import background_engagement_worker
from app.handlers.owner_handlers import (
    _build_admin_panel_payload,
    on_owner_menu_chats,
    on_owner_menu_stats,
    on_owner_menu_back,
)


def test_settings_grok_attributes():
    """Проверка наличия полей Grok AI и Groq Cloud в Settings."""
    settings = Settings(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        grok_api_key="test-grok-key",
    )
    assert settings.grok_api_key_value == "test-grok-key"
    assert settings.grok_model == "grok-beta"
    assert "x.ai" in settings.grok_api_url

    # Проверка настроек Groq
    groq_settings = Settings(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        groq_api_key="gsk_test_groq_key_12345",
    )
    assert groq_settings.groq_api_key_value == "gsk_test_groq_key_12345"
    assert groq_settings.effective_ai_api_key == "gsk_test_groq_key_12345"
    assert "api.groq.com" in groq_settings.effective_ai_api_url
    assert groq_settings.effective_ai_model == "openai/gpt-oss-120b"


def test_ai_trigger_grok_and_pm():
    """Проверка срабатывания триггеров на 'грок' и в личных сообщениях."""
    bot_id = 999000
    now = datetime.now(timezone.utc)

    # 1. Триггер 'грок' в группе
    msg_grok = types.Message(
        message_id=1,
        date=now,
        chat=types.Chat(id=-1001, type=ChatType.SUPERGROUP, title="TestGroup"),
        text="Грок, какая сегодня погода?",
    )
    assert _should_trigger_ai(msg_grok, bot_id) is True

    # 2. Триггер 'grok' на латинице
    msg_grok_en = types.Message(
        message_id=2,
        date=now,
        chat=types.Chat(id=-1001, type=ChatType.SUPERGROUP, title="TestGroup"),
        text="tell me a joke, grok",
    )
    assert _should_trigger_ai(msg_grok_en, bot_id) is True

    # 3. В ЛС (PM) - обычный вопрос без ключевого слова
    msg_pm_question = types.Message(
        message_id=3,
        date=now,
        chat=types.Chat(id=12345, type=ChatType.PRIVATE),
        text="Как дела?",
    )
    assert _should_trigger_ai(msg_pm_question, bot_id) is True

    # 4. В ЛС (PM) - техническая команда не должна триггерить Grok
    msg_pm_cmd = types.Message(
        message_id=4,
        date=now,
        chat=types.Chat(id=12345, type=ChatType.PRIVATE),
        text="профиль",
    )
    assert _should_trigger_ai(msg_pm_cmd, bot_id) is False

    # 5. В ЛС (PM) - слеш-команда
    msg_pm_slash = types.Message(
        message_id=5,
        date=now,
        chat=types.Chat(id=12345, type=ChatType.PRIVATE),
        text="/start",
    )
    assert _should_trigger_ai(msg_pm_slash, bot_id) is False


def test_split_into_chunks():
    """Проверка разбиения длинных сообщений."""
    short_text = "Короткий ответ"
    assert _split_into_chunks(short_text, max_length=100) == [short_text]

    long_text = "Слово " * 200  # Длина > 1000
    chunks = _split_into_chunks(long_text, max_length=200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 200


@pytest.mark.asyncio
async def test_send_chunk_html_fallback():
    """Проверка того, что при ошибке HTML parse_mode происходит фоллбек на plain text."""
    reply_success = AsyncMock()
    message_mock = AsyncMock(spec=types.Message)
    # Первый вызов message.reply с HTML вызывает ошибку, второй без parse_mode успешен
    message_mock.reply = AsyncMock(side_effect=[Exception("Bad Request: can't parse entities"), reply_success])

    await _send_chunk(message_mock, "Текст с <b>незакрытым тегом <bad>", is_first=True)
    assert message_mock.reply.call_count == 2
    # Второй вызов должен быть с parse_mode=None
    assert message_mock.reply.call_args_list[1][1].get("parse_mode") is None


@pytest.mark.asyncio
async def test_engagement_worker_interval_bounds():
    """Проверка диапазона интервалов фонового воркера (от 3 до 6 часов)."""
    bot_mock = AsyncMock()
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("app.tasks.background.process_dm_baits", new_callable=AsyncMock, return_value=0), \
         patch("app.tasks.background.process_chat_baits", new_callable=AsyncMock, return_value=0):

        # Вызываем один шаг цикла и прерываем
        mock_sleep.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await background_engagement_worker(
                bot_mock,
                min_interval_seconds=10800,
                max_interval_seconds=21600,
            )

        mock_sleep.assert_called_once()
        sleep_arg = mock_sleep.call_args[0][0]
        assert 10800 <= sleep_arg <= 21600


@pytest.mark.asyncio
async def test_process_chat_baits_random_chats():
    """Проверка случайной рассылки по группам."""
    async with session_scope() as session:
        # Создаем тестовые группы
        for i in range(1, 6):
            chat = Chat(
                telegram_id=-100200000 + i,
                title=f"Тестовый чат #{i}",
                chat_type="supergroup",
                is_active=True,
            )
            session.add(chat)

    bot_mock = AsyncMock()
    async with session_scope() as session:
        sent = await process_chat_baits(session, bot_mock, max_chats=2)
        assert sent == 2
        assert bot_mock.send_message.call_count == 2


@pytest.mark.asyncio
async def test_owner_admin_panel_chat_display():
    """Проверка отображения списка чатов в админ-панели владельца."""
    owner_id = 8039111975
    async with session_scope() as session:
        chat1 = Chat(
            telegram_id=-100901,
            title="Основной Чат Fable",
            chat_type="supergroup",
            is_active=True,
        )
        chat2 = Chat(
            telegram_id=-100902,
            title="Архивный Чат",
            chat_type="group",
            is_active=False,
        )
        session.add(chat1)
        session.add(chat2)

    # 1. Проверяем генерацию главной панели
    text, markup = await _build_admin_panel_payload(owner_id)
    assert "Основной Чат Fable" in text
    assert "-100901" in text
    assert "Архивный Чат" in text
    assert "🟢" in text
    assert "🔴" in text

    # 2. Проверяем callback 'owner_menu_chats'
    message_mock = AsyncMock(spec=types.Message)
    message_mock.edit_text = AsyncMock()
    callback_mock = AsyncMock(spec=types.CallbackQuery)
    callback_mock.from_user = types.User(id=owner_id, is_bot=False, first_name="Owner")
    callback_mock.message = message_mock
    callback_mock.data = "owner_menu_chats"

    await on_owner_menu_chats(callback_mock)
    message_mock.edit_text.assert_called_once()
    chats_text = message_mock.edit_text.call_args[0][0]
    assert "Основной Чат Fable" in chats_text
    assert "-100901" in chats_text
    assert "Архивный Чат" in chats_text

    # 3. Проверяем callback 'owner_menu_stats'
    message_mock.edit_text.reset_mock()
    callback_mock.data = "owner_menu_stats"
    await on_owner_menu_stats(callback_mock)
    message_mock.edit_text.assert_called_once()
    stats_text = message_mock.edit_text.call_args[0][0]
    assert "Чаты с ботом:" in stats_text
    assert "Основной Чат Fable" in stats_text

    # 4. Проверяем callback 'owner_menu_back'
    message_mock.edit_text.reset_mock()
    callback_mock.data = "owner_menu_back"
    await on_owner_menu_back(callback_mock)
    message_mock.edit_text.assert_called_once()
    back_text = message_mock.edit_text.call_args[0][0]
    assert "ГЛАВНАЯ АДМИН-ПАНЕЛЬ ВЛАДЕЛЬЦА FABLE" in back_text
