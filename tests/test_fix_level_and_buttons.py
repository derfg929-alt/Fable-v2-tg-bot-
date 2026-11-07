import pytest
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from aiogram import types
from aiogram.enums import ChatType

from app.db.models import Base
from app.db.session import _migrate_legacy_schema
from app.services.player_service import get_or_create_player
from app.main import create_dispatcher
from app.handlers.extended_moderation_handlers import on_report_accept, on_report_reject, on_preset_select
from app.handlers.games_handlers import on_marry_accept_click, on_duel_accept, on_duel_decline


@pytest.mark.asyncio
async def test_legacy_players_level_migration():
    """Проверяет корректность автоматической миграции, если в таблице players есть legacy NOT NULL колонка level."""
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with test_engine.begin() as conn:
        # Создаем таблицы из метаданных Base
        await conn.run_sync(Base.metadata.create_all)
        # Добавляем устаревшую колонку level с NOT NULL ограничением
        await conn.execute(text("ALTER TABLE players ADD COLUMN level INTEGER NOT NULL DEFAULT 1"))
        # Запускаем процедуру миграции схемы
        await _migrate_legacy_schema(conn)

    factory = async_sessionmaker(bind=test_engine, class_=AsyncSession)
    async with factory() as session:
        async with session.begin():
            # Регистрация игрока без передачи level должна завершаться успешно
            reg = await get_or_create_player(
                session,
                telegram_id=8039111975,
                first_name="Som",
                username="Som_teg",
            )
            assert reg.player.telegram_id == 8039111975
            assert reg.player.first_name == "Som"
            assert reg.player.username == "Som_teg"

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_callback_handlers_always_answer():
    """Проверяет, что обработчики кнопок всегда вызывают callback.answer(), предотвращая зависание спиннера."""
    chat_mock = types.Chat(id=-10012345, type=ChatType.SUPERGROUP, title="Test Chat")
    user_mock = types.User(id=8039111975, is_bot=False, first_name="Som")
    message_mock = AsyncMock(spec=types.Message)
    message_mock.chat = chat_mock
    message_mock.edit_text = AsyncMock()

    callback_mock = AsyncMock(spec=types.CallbackQuery)
    callback_mock.message = message_mock
    callback_mock.from_user = user_mock
    callback_mock.answer = AsyncMock()

    # Preset selection error scenario (non-existent chat/preset)
    callback_mock.data = "preset_strict_-999999"
    await on_preset_select(callback_mock)
    callback_mock.answer.assert_awaited()

    # Report accept error scenario (non-existent report)
    callback_mock.answer.reset_mock()
    callback_mock.data = "rep_acc_999999"
    await on_report_accept(callback_mock)
    callback_mock.answer.assert_awaited()

    # Report reject error scenario
    callback_mock.answer.reset_mock()
    callback_mock.data = "rep_rej_999999"
    await on_report_reject(callback_mock)
    callback_mock.answer.assert_awaited()

    # Duel accept error scenario (non-existent duel)
    callback_mock.answer.reset_mock()
    callback_mock.data = "duel_acc_999999"
    await on_duel_accept(callback_mock)
    callback_mock.answer.assert_awaited()

    # Duel decline error scenario
    callback_mock.answer.reset_mock()
    callback_mock.data = "duel_dec_999999"
    await on_duel_decline(callback_mock)
    callback_mock.answer.assert_awaited()
