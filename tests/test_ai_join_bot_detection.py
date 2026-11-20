"""Тесты ИИ-модерации на входе: автоматический бан ботов по профилю.

Раньше на входе проверялся только «рекламный» ник из узкого списка шаблонов,
и любой бот с ником вида «xQ7fK2pL9» спокойно заходил и начинал рассылку.
Теперь профиль оценивает classify_join_profile: сначала эвристики, затем LLM.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.db.models import ModerationCase
from app.db.session import session_scope
from app.services import ai_moderation_service
from app.services.ai_moderation_service import (
    _parse_llm_verdict,
    classify_join_profile,
    looks_like_random_handle,
)
from app.services.chat_settings_service import DEFAULT_CHAT_SETTINGS, get_or_create_chat
from app.services.player_service import get_or_create_player


def _join_event(chat_id: int, user_id: int, first_name: str, username: str | None, is_bot: bool = False):
    event = MagicMock()
    event.chat = MagicMock(id=chat_id, title="Join")
    event.new_chat_member = MagicMock(
        user=MagicMock(
            id=user_id, is_bot=is_bot, first_name=first_name, username=username, last_name=None
        )
    )
    return event


# ===================== ЭВРИСТИКИ =====================

def test_random_handle_detected():
    assert looks_like_random_handle("user8271364", "user8271364")
    assert looks_like_random_handle("Аккаунт", "xQ7fK2pL9z")
    assert looks_like_random_handle("Иван", "ivan_petrov") is None
    assert looks_like_random_handle("Анна", "anna") is None


@pytest.mark.asyncio
async def test_telegram_bot_account_always_flagged():
    verdict = await classify_join_profile(first_name="Any", is_bot=True)
    assert verdict.is_spam and verdict.score == 1.0


@pytest.mark.asyncio
async def test_spam_nick_flagged_without_llm_call(monkeypatch):
    """Рекламный ник решается эвристикой — токены модели не тратятся."""
    called = AsyncMock()
    monkeypatch.setattr(ai_moderation_service.ai_service, "ask_grok", called)

    verdict = await classify_join_profile(
        first_name="Free Money", username="freemoney_airdrop", settings={"ai_moderation_use_llm": True}
    )
    assert verdict.is_spam
    assert verdict.source == "heuristic"
    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_person_not_flagged_when_llm_says_human(monkeypatch):
    monkeypatch.setattr(
        ai_moderation_service.ai_service, "ask_grok", AsyncMock(return_value="HUMAN 0 обычный человек")
    )
    verdict = await classify_join_profile(
        first_name="Анна", username="anna_ivanova", settings={"ai_moderation_use_llm": True}
    )
    assert not verdict.is_spam


@pytest.mark.asyncio
async def test_llm_can_flag_suspicious_profile(monkeypatch):
    monkeypatch.setattr(
        ai_moderation_service.ai_service,
        "ask_grok",
        AsyncMock(return_value="BOT 0.9 бессмысленный ник и нет фамилии"),
    )
    verdict = await classify_join_profile(
        first_name="Zyx", username="zyxwv", settings={"ai_moderation_use_llm": True}
    )
    assert verdict.is_spam
    assert verdict.source == "llm"


@pytest.mark.asyncio
async def test_llm_failure_does_not_ban(monkeypatch):
    """Ошибка модели не должна приводить к ложному бану живого человека."""
    monkeypatch.setattr(
        ai_moderation_service.ai_service,
        "ask_grok",
        AsyncMock(side_effect=RuntimeError("нет связи")),
    )
    verdict = await classify_join_profile(
        first_name="Пётр", username="petr", settings={"ai_moderation_use_llm": True}
    )
    assert not verdict.is_spam


def test_llm_parser_understands_bot_and_human_labels():
    assert _parse_llm_verdict("BOT 0.9 спам-ник") == (True, 0.9, "спам-ник")
    assert _parse_llm_verdict("HUMAN 0 живой") == (False, 0.0, "живой")
    assert _parse_llm_verdict("SPAM 0.8 реклама") == (True, 0.8, "реклама")
    assert _parse_llm_verdict("мусор") is None


# ===================== ИНТЕГРАЦИЯ С ОБРАБОТЧИКОМ ВХОДА =====================

@pytest.mark.asyncio
async def test_auto_ban_records_moderation_case():
    """Авто-бан бота не только выкидывает его, но и попадает в историю наказаний."""
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    chat_id = -100940001
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="AI Join")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = True
        chat.settings = settings
        await session.flush()

    bot = MagicMock()
    bot.id = 777
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(
        _join_event(chat_id, 940001, "Crypto Signals", "cryptosig_bot"), bot
    )

    bot.ban_chat_member.assert_awaited_once()
    assert bot.ban_chat_member.call_args.kwargs["user_id"] == 940001

    async with session_scope() as session:
        case = await session.scalar(
            select(ModerationCase).where(ModerationCase.target_id == 940001)
        )
        assert case is not None
        assert case.action == "ban"
        assert "Проверка входа" in case.reason


@pytest.mark.asyncio
async def test_random_handle_account_banned_when_ai_enabled():
    """Ник из случайных символов — достаточный повод, даже без рекламных слов."""
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    chat_id = -100940002
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="AI Join 2")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = True
        settings["ai_moderation_use_llm"] = False
        chat.settings = settings
        await session.flush()

    bot = MagicMock()
    bot.id = 777
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(_join_event(chat_id, 940002, "user8271364", "user8271364"), bot)

    bot.ban_chat_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_check_can_be_disabled_by_admin():
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    chat_id = -100940003
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="AI Join 3")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings["ai_moderation_enabled"] = False
        settings["join_check_enabled"] = False
        settings["welcome_enabled"] = False
        chat.settings = settings
        await session.flush()
        await get_or_create_player(session, telegram_id=940003, first_name="Free Money")

    bot = MagicMock()
    bot.id = 777
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(_join_event(chat_id, 940003, "Free Money", "freemoney"), bot)

    bot.ban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_protection_works_out_of_the_box(monkeypatch):
    """Защита включена по умолчанию: админу не нужно ничего настраивать.

    Проверка профиля работает на эвристиках и не обращается к нейросети,
    поэтому не тратит токены и не требует API-ключа.
    """
    from app.handlers.extended_moderation_handlers import on_new_chat_member

    llm = AsyncMock()
    monkeypatch.setattr(ai_moderation_service.ai_service, "ask_grok", llm)

    chat_id = -100940004
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Default Join")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        settings.pop("join_check_enabled", None)
        settings.pop("ai_moderation_enabled", None)
        chat.settings = settings
        await session.flush()

    bot = MagicMock()
    bot.id = 777
    bot.get_me = AsyncMock(return_value=MagicMock(id=777))
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    bot.send_message = AsyncMock()

    await on_new_chat_member(_join_event(chat_id, 940004, "Free Money", "freemoney_airdrop"), bot)

    bot.ban_chat_member.assert_awaited_once()
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_looking_people_are_not_banned():
    """Обычные ники не должны попадать под автобан."""
    for username, first_name in [
        ("ivan_petrov", "Иван"),
        ("dmitry", "Дмитрий"),
        ("anna_1990", "Анна"),
        ("sergey", "Сергей"),
        ("masha", "Мария"),
    ]:
        verdict = await classify_join_profile(
            first_name=first_name, username=username, settings={"ai_moderation_use_llm": False}
        )
        assert not verdict.is_spam, f"{username} ошибочно принят за бота: {verdict.reason}"
