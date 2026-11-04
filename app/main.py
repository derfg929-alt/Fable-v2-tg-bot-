"""Главная точка входа Telegram-бота Fable (aiogram 3)."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import get_settings
from app.db.session import check_database_connection, dispose_engine, engine

# --- УМНЫЙ ПОИСК BASE ---
try:
    from app.db.base import Base
except ImportError:
    try:
        from app.models import Base
    except ImportError:
        try:
            from app.db.models import Base
        except ImportError:
            from sqlalchemy.orm import declarative_base
            Base = declarative_base()
# -------------------------

from app.handlers.economy_handlers import router as economy_router
from app.handlers.exchange_handlers import router as exchange_router
from app.handlers.games_handlers import router as games_router
from app.handlers.social_moderation_handlers import router as social_moderation_router
from app.handlers.extended_moderation_handlers import router as extended_moderation_router
from app.handlers.ai_handlers import router as ai_router
from app.middlewares import MessageActivityAndSecurityMiddleware
from app.handlers.owner_handlers import router as owner_router
from app.tasks.background import (
    background_bank_interest_worker,
    background_market_maker_worker,
    background_engagement_worker,
)

logger = logging.getLogger("fable")


def create_dispatcher() -> Dispatcher:
    """Создает и настраивает Dispatcher aiogram 3 со всеми роутерами."""
    dp = Dispatcher()
    
    # --- ОБРАБОТЧИК START ---
    from app.services.help_service import get_greeting_text

    @dp.message(CommandStart())
    @dp.message(F.text.casefold().in_(["старт", "!старт"]))
    async def cmd_start(message: types.Message):
        await message.answer(get_greeting_text(), parse_mode="HTML")
    # ------------------------------------

    dp.include_router(owner_router)
    dp.include_router(economy_router)
    dp.include_router(exchange_router)
    dp.include_router(games_router)
    dp.include_router(social_moderation_router)
    dp.include_router(extended_moderation_router)
    dp.include_router(ai_router)
    dp.message.middleware(MessageActivityAndSecurityMiddleware())
    return dp


async def main() -> None:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    logger.info("Проверка подключения к базе данных PostgreSQL...")
    if not await check_database_connection():
        logger.critical("Не удалось подключиться к базе данных PostgreSQL!")
        sys.exit(1)
    
    logger.info("Подключение к PostgreSQL успешно.")

    # ==========================================
    # БЛОК СОЗДАНИЯ ТАБЛИЦ
    # ==========================================
    logger.info("Проверка и создание таблиц в базе данных...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Все таблицы успешно созданы/проверены.")
    except Exception as e:
        logger.error(f"Ошибка при создании таблиц: {e}")
    # ==========================================

    bot = Bot(
        token=settings.bot_token_value,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = create_dispatcher()

    # Запуск фоновых задач
    bank_task = asyncio.create_task(background_bank_interest_worker(interval_seconds=3600))
    mm_task = asyncio.create_task(background_market_maker_worker(interval_seconds=60))
    engagement_task = asyncio.create_task(background_engagement_worker(bot, interval_seconds=7200))

    logger.info("Запуск Telegram-бота Fable в режиме Long Polling...")
    try:
        await dp.start_polling(bot, polling_timeout=settings.polling_timeout)
    finally:
        bank_task.cancel()
        mm_task.cancel()
        engagement_task.cancel()
        await bot.session.close()
        await dispose_engine()
        logger.info("Бот Fable успешно остановлен.")


if __name__ == "__main__":
    asyncio.run(main())