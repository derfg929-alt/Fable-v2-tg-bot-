"""Тесты ИИ-модерации спама, статистики профиля и подсказок для команд.

Покрываем то, на что жаловались пользователи:
- профиль не показывал, сколько человек в боте и сколько он написал;
- кнопки/команды модерации молча ничего не отвечали в личных сообщениях;
- не было автоматического бана/удаления ботов и рекламы.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.enums import ChatType

from app.db.models import ActivityRecord, Chat, Player
from app.db.session import session_scope
from app.handlers.common_hints import GROUP_ONLY_HINT, OWNER_ONLY_HINT
from app.services.activity_service import get_global_activity_breakdown
from app.services.ai_moderation_service import (
    classify_message,
    cleanup_expired_captchas,
    looks_like_bot_name,
    _heuristic_score,
)
from app.services.chat_settings_service import (
    DEFAULT_CHAT_SETTINGS,
    create_captcha,
    get_or_create_chat,
)

CHAT_A = -100900001
CHAT_B = -100900002


def _msg(text: str, *, chat_type=ChatType.SUPERGROUP, chat_id=CHAT_A, user_id=900001):
    """Лёгкий мок сообщения: aiogram.Message заморожен и не даёт подменить reply."""
    message = MagicMock()
    message.message_id = 1
    message.date = datetime.now(timezone.utc)
    message.chat = types.Chat(id=chat_id, type=chat_type, title="Test")
    message.from_user = types.User(id=user_id, is_bot=False, first_name="Игрок")
    message.text = text
    message.reply_to_message = None
    message.bot = MagicMock(id=777)
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    return message


# ===================== ИИ-МОДЕРАЦИЯ =====================

def test_heuristic_detects_invite_link_spam():
    score, labels = _heuristic_score("Заходи в наш чат t.me/joinchat/AbCdEf123456 — там заработок")
    assert score >= 0.6
    assert labels


def test_heuristic_ignores_normal_chat():
    score, labels = _heuristic_score("Привет, как дела? Кто идёт сегодня играть в футбол?")
    assert score == 0.0
    assert labels == []


def test_heuristic_detects_casino_ad():
    score, _ = _heuristic_score("1xbet казино — ставки на спорт с бонусом!")
    assert score >= 0.6


@pytest.mark.asyncio
async def test_classify_message_blocks_spam_without_llm():
    """Явный спам отсекается эвристиками, без обращения к модели."""
    verdict = await classify_message(
        "Заработок от 50000 в день! Пиши @moneymaker в ЛС",
        settings={"ai_moderation_min_score": 0.6, "ai_moderation_use_llm": False},
    )
    assert verdict.is_spam is True
    assert verdict.source == "heuristic"


@pytest.mark.asyncio
async def test_classify_message_passes_clean_text():
    verdict = await classify_message(
        "Ребята, кто знает, когда следующий конкурс?",
        settings={"ai_moderation_min_score": 0.6},
    )
    assert verdict.is_spam is False


@pytest.mark.asyncio
async def test_classify_message_uses_llm_for_borderline(monkeypatch):
    """Подозрительное, но не явное сообщение уходит на проверку модели."""
    called = {}

    async def fake_ask(prompt, user_name=None):
        called["prompt"] = prompt
        return "SPAM 0.9 реклама стороннего канала"

    monkeypatch.setattr("app.services.ai_moderation_service.ai_service.ask_grok", fake_ask)

    verdict = await classify_message(
        "Подпишись на канал, там промокод BONUS12345",
        settings={"ai_moderation_min_score": 0.6, "ai_moderation_use_llm": True},
    )
    assert "prompt" in called
    assert verdict.is_spam is True
    assert verdict.source == "llm"


@pytest.mark.asyncio
async def test_classify_message_survives_llm_failure(monkeypatch):
    async def boom(prompt, user_name=None):
        raise RuntimeError("нет сети")

    monkeypatch.setattr("app.services.ai_moderation_service.ai_service.ask_grok", boom)

    verdict = await classify_message(
        "Подпишись на канал, там промокод BONUS12345",
        settings={"ai_moderation_min_score": 0.6, "ai_moderation_use_llm": True},
    )
    # Ошибка ИИ не должна превращаться в ложный бан.
    assert verdict.is_spam is False


def test_looks_like_bot_name():
    assert looks_like_bot_name("Crypto Signals", "cryptosig_bot")
    assert looks_like_bot_name("Free Money", None)
    assert looks_like_bot_name("Анна", "anna_ivanova") is None


def test_default_settings_include_ai_moderation():
    assert DEFAULT_CHAT_SETTINGS["ai_moderation_enabled"] is False
    assert DEFAULT_CHAT_SETTINGS["ai_moderation_action"] == "delete"
    assert DEFAULT_CHAT_SETTINGS["kick_bots_on_join"] is True


# ===================== КАПЧА =====================

@pytest.mark.asyncio
async def test_expired_captcha_is_closed_by_cleanup():
    """Просроченная капча не должна блокировать участника вечно."""
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=CHAT_A, title="Cap")
        challenge = await create_captcha(session, chat_id=CHAT_A, user_id=900010)
        challenge.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    async with session_scope() as session:
        closed = await cleanup_expired_captchas(session)
        assert closed == 1

    async with session_scope() as session:
        again = await cleanup_expired_captchas(session)
        assert again == 0


# ===================== ПРОФИЛЬ: СТАТИСТИКА =====================

@pytest.mark.asyncio
async def test_global_activity_counts_messages_across_chats():
    uid = 900020
    async with session_scope() as session:
        session.add(Player(telegram_id=uid, first_name="Активный"))
        for chat_id in (CHAT_A, CHAT_B):
            session.add(Chat(telegram_id=chat_id, title=f"Чат {chat_id}", settings={}))
        await session.flush()
        for i in range(3):
            session.add(ActivityRecord(chat_id=CHAT_A, user_id=uid, points=1, message_length=10))
        for i in range(2):
            session.add(ActivityRecord(chat_id=CHAT_B, user_id=uid, points=1, message_length=10))

    async with session_scope() as session:
        act = await get_global_activity_breakdown(session, user_id=uid)

    assert act["all_time"] == 5
    assert act["day"] == 5
    assert act["chats"] == 2
    assert len(act["top_chats"]) == 2
    assert act["top_chats"][0]["messages"] == 3


@pytest.mark.asyncio
async def test_global_activity_empty_for_new_player():
    async with session_scope() as session:
        act = await get_global_activity_breakdown(session, user_id=999999)
    assert act["all_time"] == 0
    assert act["chats"] == 0
    assert act["last_active"] is None
    assert act["top_chats"] == []


# ===================== ПОДСКАЗКИ ВМЕСТО МОЛЧАНИЯ =====================

@pytest.mark.asyncio
async def test_group_only_command_replies_hint_in_private():
    from app.handlers.social_moderation_handlers import handle_mute

    message = _msg("мут 30м", chat_type=ChatType.PRIVATE, chat_id=900030)

    await handle_mute(message)

    message.reply.assert_awaited_once()
    assert "только в группе" in message.reply.call_args[0][0]


def test_hint_templates_are_formatted():
    assert "{cmd}" in GROUP_ONLY_HINT
    assert "{cmd}" in OWNER_ONLY_HINT


@pytest.mark.asyncio
async def test_ai_moderation_command_help_in_group():
    from app.handlers.extended_moderation_handlers import handle_ai_moderation_settings

    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=CHAT_A, title="Test")
        settings = dict(chat.settings or {})
        settings["test_admin"] = True
        chat.settings = settings

    message = _msg("ии")

    bot = MagicMock()
    bot.id = 777
    with pytest.MonkeyPatch.context() as mp:
        async def fake_level(session, *, chat_id, user_id, bot=None):
            return 4

        mp.setattr("app.handlers.extended_moderation_handlers.get_user_admin_level", fake_level)
        await handle_ai_moderation_settings(message, bot)

    message.reply.assert_awaited_once()
    assert "ИИ-модерация" in message.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_ai_moderation_command_toggles_setting():
    from app.handlers.extended_moderation_handlers import handle_ai_moderation_settings

    message = _msg("ии вкл")

    bot = MagicMock()
    bot.id = 777
    with pytest.MonkeyPatch.context() as mp:
        async def fake_level(session, *, chat_id, user_id, bot=None):
            return 4

        mp.setattr("app.handlers.extended_moderation_handlers.get_user_admin_level", fake_level)
        await handle_ai_moderation_settings(message, bot)

    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=CHAT_A, title="Test")
        assert (chat.settings or {})["ai_moderation_enabled"] is True


# ===================== МИДЛВАРЬ: УДАЛЕНИЕ СПАМА =====================

def _group_message(chat_id: int, user_id: int, text: str):
    msg = MagicMock(spec=types.Message)
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="MW Chat")
    msg.from_user = MagicMock(id=user_id, is_bot=False, first_name="Спамер", username="spam")
    msg.text = text
    msg.caption = None
    msg.message_id = 777
    msg.bot = MagicMock()
    msg.bot.id = 999000222
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_middleware_deletes_spam_when_ai_moderation_enabled():
    """Спам удаляется автоматически, и хэндлеры его уже не видят."""
    from app.middlewares import MessageActivityAndSecurityMiddleware

    chat_id = -100900101
    user_id = 900101
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="AI Chat")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = True
        settings["ai_moderation_use_llm"] = False
        settings["ai_moderation_action"] = "delete"
        chat.settings = settings
        await session.flush()

    mw = MessageActivityAndSecurityMiddleware()
    called = {"handler": False}

    async def handler(event, data):
        called["handler"] = True
        return "handled"

    msg = _group_message(chat_id, user_id, "Заработок от 50000 в день! Пиши @moneymaker в ЛС")
    result = await mw(handler, msg, {})

    assert called["handler"] is False
    assert result is None
    assert msg.delete.called


@pytest.mark.asyncio
async def test_middleware_passes_clean_message_when_ai_moderation_enabled():
    from app.middlewares import MessageActivityAndSecurityMiddleware

    chat_id = -100900102
    user_id = 900102
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="AI Chat 2")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = True
        settings["ai_moderation_use_llm"] = False
        chat.settings = settings
        await session.flush()

    mw = MessageActivityAndSecurityMiddleware()

    async def handler(event, data):
        return "handled"

    msg = _group_message(chat_id, user_id, "Всем привет! Когда следующий розыгрыш?")
    assert await mw(handler, msg, {}) == "handled"
    assert not msg.delete.called


@pytest.mark.asyncio
async def test_ai_moderation_disabled_by_default_keeps_messages():
    """По умолчанию ИИ-модерация выключена — обычные сообщения не удаляются."""
    from app.middlewares import MessageActivityAndSecurityMiddleware

    chat_id = -100900103
    user_id = 900103
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Default Chat")
        await session.flush()

    mw = MessageActivityAndSecurityMiddleware()

    async def handler(event, data):
        return "handled"

    msg = _group_message(chat_id, user_id, "Заработок от 50000 в день! Пиши @moneymaker в ЛС")
    assert await mw(handler, msg, {}) == "handled"
    assert not msg.delete.called


# ===================== СПАМ-НИКИ И ВХОД БОТОВ =====================

@pytest.mark.asyncio
async def test_middleware_removes_spam_nick_message():
    """Ник «Crypto Signals» ловится даже когда текст сам по себе безобиден."""
    from app.middlewares import MessageActivityAndSecurityMiddleware

    chat_id = -100900104
    user_id = 900104
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Nick Chat")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = True
        settings["ai_moderation_use_llm"] = False
        chat.settings = settings
        await session.flush()

    mw = MessageActivityAndSecurityMiddleware()

    async def handler(event, data):
        return "handled"

    msg = _group_message(chat_id, user_id, "Всем привет, рад быть здесь")
    msg.from_user.first_name = "Crypto Signals"
    msg.from_user.username = "cryptosig_promo"

    assert await mw(handler, msg, {}) is None
    assert msg.delete.called


def _join_event(chat_id: int, user_id: int, first_name: str, username=None, is_bot=False):
    event = MagicMock()
    event.chat = MagicMock(id=chat_id, title="Join Chat")
    event.new_chat_member = MagicMock(
        user=MagicMock(id=user_id, is_bot=is_bot, first_name=first_name, username=username)
    )
    return event


@pytest.mark.asyncio
async def test_bot_account_is_kicked_on_join():
    """Любой добавленный бот выкидывается, чтобы реклама не попадала в чат."""
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    chat_id = -100900105
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Join Chat")
        await session.flush()

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(_join_event(chat_id, 555001, "SpamBot", "spam_bot", is_bot=True), bot)

    bot.ban_chat_member.assert_awaited_once()
    assert bot.ban_chat_member.call_args.kwargs["user_id"] == 555001


@pytest.mark.asyncio
async def test_bot_kick_can_be_disabled():
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    chat_id = -100900106
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Join Chat 2")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["kick_bots_on_join"] = False
        chat.settings = settings
        await session.flush()

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(_join_event(chat_id, 555002, "SpamBot", "spam_bot", is_bot=True), bot)

    bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_spam_nick_account_kicked_on_join_when_ai_enabled():
    """Человеческий аккаунт с рекламным ником тоже отсекается ИИ-модерацией."""
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    chat_id = -100900107
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Join Chat 3")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = True
        chat.settings = settings
        await session.flush()

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(_join_event(chat_id, 555003, "Free Money", "freemoney_airdrop"), bot)

    bot.ban_chat_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_user_joins_without_ban():
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    chat_id = -100900108
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Join Chat 4")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = True
        settings["welcome_enabled"] = True
        chat.settings = settings
        await session.flush()

    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(_join_event(chat_id, 555004, "Анна", "anna_ivanova"), bot)

    bot.ban_chat_member.assert_not_awaited()
    bot.send_message.assert_awaited()
