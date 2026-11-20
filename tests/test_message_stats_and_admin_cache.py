"""Тесты статистики сообщений и кэша прав администраторов.

1. «Сколько всего сообщений» раньше считалось по ActivityRecord, которые
   создавались только при начислении монет (кулдаун 60 с, лимит 40/день,
   минимум 12 символов) — профиль показывал в разы меньше реального.
2. get_user_admin_level запрашивал bot.get_chat_member на каждое сообщение
   каждого участника; Telegram отвечает на такой поток 429 Too Many Requests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types

from app.db.models import ActivityRecord
from app.db.session import session_scope
from app.middlewares import MessageActivityAndSecurityMiddleware
from app.services.activity_service import (
    get_global_activity_breakdown,
    record_message_activity,
    set_activity_settings,
)
from app.services.chat_settings_service import get_or_create_chat
from app.services.moderation_service import (
    get_user_admin_level,
    invalidate_admin_cache,
    set_admin_level,
)
from app.services.player_service import get_or_create_player

CHAT_ID = -100920001
USER_ID = 920001


def _message(chat_id: int, user_id: int, message_id: int, text: str) -> MagicMock:
    msg = MagicMock(spec=types.Message)
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="Stats")
    msg.from_user = MagicMock(id=user_id, is_bot=False, first_name="Аня", username="anya")
    msg.text = text
    msg.caption = None
    msg.message_id = message_id
    msg.bot = MagicMock()
    msg.bot.id = 999
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


# ===================== СТАТИСТИКА СООБЩЕНИЙ =====================

@pytest.mark.asyncio
async def test_every_message_counted_even_when_not_rewarded():
    """Короткие сообщения и сообщения в кулдаун тоже попадают в статистику."""
    chat_id = CHAT_ID
    user_id = USER_ID
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Stats")
        await get_or_create_player(session, telegram_id=user_id, first_name="Аня")
        # Лимиты заведомо жёсткие: монеты получит максимум одно сообщение.
        await set_activity_settings(
            session,
            values={"coins_per_message": 10, "cooldown_seconds": 3600, "minimum_length": 12},
        )
        await session.flush()

    middleware = MessageActivityAndSecurityMiddleware()

    async def handler(event, data):
        return "handled"

    texts = ["ок", "привет", "как дела", "это достаточно длинное сообщение", "ещё одно сообщение"]
    for index, text in enumerate(texts, start=1):
        await middleware(handler, _message(chat_id, user_id, index, text), {})

    async with session_scope() as session:
        stats = await get_global_activity_breakdown(session, user_id=user_id)
        assert stats["all_time"] == len(texts)
        assert stats["day"] == len(texts)

        rewarded = await session.scalar(
            ActivityRecord.__table__.select().where(ActivityRecord.reward_coins > 0)
        )
        assert rewarded is not None, "хотя бы одно сообщение должно быть оплачено"


@pytest.mark.asyncio
async def test_same_message_not_counted_twice():
    """Повторная обработка одного message_id (ретрай апдейта) не удваивает счётчик."""
    chat_id = CHAT_ID - 1
    user_id = USER_ID + 1
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Stats2")
        await get_or_create_player(session, telegram_id=user_id, first_name="Боря")
        await session.flush()
        assert await record_message_activity(
            session, chat_id=chat_id, user_id=user_id, message_id=42, text="привет"
        )
        assert not await record_message_activity(
            session, chat_id=chat_id, user_id=user_id, message_id=42, text="привет"
        )


@pytest.mark.asyncio
async def test_cooldown_ignores_unrewarded_messages():
    """Короткое сообщение не должно сдвигать кулдаун награды."""
    from app.services.activity_service import reward_chat_message

    chat_id = CHAT_ID - 2
    user_id = USER_ID + 2
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Stats3")
        await get_or_create_player(session, telegram_id=user_id, first_name="Вера")
        await set_activity_settings(
            session,
            values={"coins_per_message": 10, "cooldown_seconds": 60, "minimum_length": 12},
        )
        await session.flush()

        short = await reward_chat_message(
            session, chat_id=chat_id, user_id=user_id, message_id=1, text="ок"
        )
        assert not short.awarded and short.reason == "message_too_short"

        first = await reward_chat_message(
            session,
            chat_id=chat_id,
            user_id=user_id,
            message_id=2,
            text="достаточно длинное сообщение",
        )
        assert first.awarded


# ===================== КЭШ ПРАВ =====================

@pytest.mark.asyncio
async def test_non_admin_lookup_is_cached():
    """Повторные проверки обычного участника не дёргают Telegram API."""
    chat_id = CHAT_ID - 3
    user_id = USER_ID + 3
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Cache")
        await get_or_create_player(session, telegram_id=user_id, first_name="Гость")
        await session.flush()

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        assert await get_user_admin_level(session, chat_id=chat_id, user_id=user_id, bot=bot) == 1
        assert await get_user_admin_level(session, chat_id=chat_id, user_id=user_id, bot=bot) == 1
        assert await get_user_admin_level(session, chat_id=chat_id, user_id=user_id, bot=bot) == 1

        assert bot.get_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_promotion_applies_immediately_despite_cache():
    """После назначения админом права видны сразу, кэш «не админ» сброшен."""
    chat_id = CHAT_ID - 4
    owner_id = USER_ID + 4
    target_id = USER_ID + 5
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Cache2")
        await get_or_create_player(session, telegram_id=owner_id, first_name="Владелец")
        await get_or_create_player(session, telegram_id=target_id, first_name="Новый")
        await session.flush()

        bot = MagicMock()

        async def _member(chat_id, user_id):
            status = "creator" if user_id == owner_id else "member"
            return MagicMock(status=status)

        bot.get_chat_member = AsyncMock(side_effect=_member)

        # Прогреваем кэш «не админ».
        assert await get_user_admin_level(session, chat_id=chat_id, user_id=target_id, bot=bot) == 1

        await set_admin_level(
            session,
            chat_id=chat_id,
            actor_id=owner_id,
            target_id=target_id,
            new_level=3,
            bot=bot,
        )

        assert await get_user_admin_level(session, chat_id=chat_id, user_id=target_id, bot=bot) == 3


@pytest.mark.asyncio
async def test_invalidate_admin_cache_allows_recheck():
    chat_id = CHAT_ID - 5
    user_id = USER_ID + 6
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Cache3")
        await get_or_create_player(session, telegram_id=user_id, first_name="Кто-то")
        await session.flush()

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        await get_user_admin_level(session, chat_id=chat_id, user_id=user_id, bot=bot)

        invalidate_admin_cache(chat_id, user_id)
        bot.get_chat_member.return_value = MagicMock(status="creator")

        assert await get_user_admin_level(session, chat_id=chat_id, user_id=user_id, bot=bot) == 5


# ===================== МИГРАЦИЯ ЛЕГАСИ-БАЗЫ =====================

@pytest.mark.asyncio
async def test_legacy_activity_table_gets_message_id_and_index(tmp_path, monkeypatch):
    """Обновление на существующей базе не должно ломать статистику сообщений.

    create_all не трогает уже созданные таблицы, поэтому колонку message_id и
    индекс дедупликации нужно доставлять вручную — иначе на «живой» базе
    запись статистики падает с «no such column».
    """
    import sqlite3

    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE players (telegram_id BIGINT PRIMARY KEY, first_name VARCHAR(64) NOT NULL,
                              wallet BIGINT NOT NULL DEFAULT 0);
        CREATE TABLE chats (telegram_id BIGINT PRIMARY KEY, title VARCHAR(255), settings JSON);
        CREATE TABLE activity_records (
            id INTEGER PRIMARY KEY, chat_id BIGINT, user_id BIGINT,
            points INTEGER NOT NULL DEFAULT 1, message_length INTEGER NOT NULL DEFAULT 0,
            reward_coins BIGINT NOT NULL DEFAULT 0, created_at DATETIME);
        """
    )
    con.commit()
    con.close()

    import app.db.session as db_session

    monkeypatch.setattr(db_session, "_initialized_db", False)
    monkeypatch.setattr(db_session, "engine", db_session.create_async_engine(f"sqlite+aiosqlite:///{db_path}"))
    try:
        await db_session.init_database()
    finally:
        await db_session.engine.dispose()

    con = sqlite3.connect(db_path)
    columns = [row[1] for row in con.execute("PRAGMA table_info(activity_records)")]
    indexes = [row[1] for row in con.execute("PRAGMA index_list(activity_records)")]
    con.close()

    assert "message_id" in columns
    assert "ix_activity_chat_user_message" in indexes
