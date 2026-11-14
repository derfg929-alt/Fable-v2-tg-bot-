"""Главная точка входа Telegram-бота Fable (aiogram 3)."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import get_settings
from app.db.session import check_database_connection, dispose_engine, engine, init_json_database

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
from app.handlers.rp_handlers import router as rp_router
from app.handlers.social_moderation_handlers import router as social_moderation_router
from app.handlers.extended_moderation_handlers import router as extended_moderation_router
from app.handlers.ai_handlers import router as ai_router
from app.handlers.promo_handlers import router as promo_router
from app.handlers.randomizer_handlers import router as randomizer_router
from app.handlers.randomizer_admin_handlers import router as randomizer_admin_router
from app.handlers.user_contest_handlers import router as user_contest_router
from app.middlewares import MessageActivityAndSecurityMiddleware, CallbackAnswerMiddleware
from app.handlers.owner_handlers import router as owner_router
from app.tasks.background import (
    background_bank_interest_worker,
    background_market_maker_worker,
    background_engagement_worker,
    background_moderation_expiry_worker,
    background_randomizer_worker,
)

logger = logging.getLogger("fable")


def create_dispatcher() -> Dispatcher:
    """Создает и настраивает Dispatcher aiogram 3 со всеми роутерами."""
    dp = Dispatcher()

    # Fable Randomizer регистрируется первым: его главное меню и /start
    # определяют основной пользовательский опыт, остальные модули — дополняют.
    all_subrouters = [
        randomizer_router,
        randomizer_admin_router,
        user_contest_router,
        owner_router,
        promo_router,
        economy_router,
        exchange_router,
        games_router,
        rp_router,
        social_moderation_router,
        extended_moderation_router,
        ai_router,
    ]
    for r in all_subrouters:
        r._parent_router = None
        dp.include_router(r)

    # Фоллбэк роутер подключается ПОСЛЕДНИМ среди всех роутеров.
    # В aiogram 3 хэндлеры на самом dp.callback_query срабатывают ДО дочерних роутеров!
    # Поэтому общий перехватчик необработанных кнопок должен находиться в последнем дочернем роутере.
    fallback_router = Router(name="fallback_callback_router")

    @fallback_router.callback_query()
    async def global_fallback_callback_handler(callback: types.CallbackQuery):
        try:
            await callback.answer()
        except Exception:
            pass

    dp.include_router(fallback_router)

    dp.message.middleware(MessageActivityAndSecurityMiddleware())
    dp.callback_query.middleware(CallbackAnswerMiddleware())

    return dp


async def main() -> None:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    logger.info("Проверка и инициализация JSON-базы данных...")
    if not await check_database_connection():
        logger.critical("Не удалось инициализировать базу данных!")
        sys.exit(1)
    
    logger.info("База данных успешно инициализирована.")

    # Показываем активное подключение к ИИ (Grok/Groq или офлайн-движок).
    from app.services.ai_service import log_ai_status

    log_ai_status()

    bot = Bot(
        token=settings.bot_token_value,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = create_dispatcher()

    # Автоматически определяем username бота через Telegram API (ТЗ 18.5),
    # чтобы реферальные ссылки формировались без ручной настройки BOT_USERNAME.
    await _ensure_bot_username(bot, settings)

    # Запуск фоновых задач
    bank_task = asyncio.create_task(background_bank_interest_worker(interval_seconds=3600))
    mm_task = asyncio.create_task(background_market_maker_worker(interval_seconds=60))
    engagement_task = asyncio.create_task(
        background_engagement_worker(bot, min_interval_seconds=10800, max_interval_seconds=21600)
    )
    moderation_expiry_task = asyncio.create_task(
        background_moderation_expiry_worker(bot, interval_seconds=60)
    )
    randomizer_task = asyncio.create_task(background_randomizer_worker(bot, interval_seconds=300))

    # Серверный API и Mini App (запускается параллельно с ботом).
    api_server = await _maybe_start_api(settings)

    logger.info("Запуск Telegram-бота Fable в режиме Long Polling...")
    try:
        await dp.start_polling(bot, polling_timeout=settings.polling_timeout)
    finally:
        bank_task.cancel()
        mm_task.cancel()
        engagement_task.cancel()
        moderation_expiry_task.cancel()
        randomizer_task.cancel()
        if api_server is not None:
            api_server.should_exit = True
        await bot.session.close()
        await dispose_engine()
        logger.info("Бот Fable успешно остановлен.")


async def _ensure_bot_username(bot, settings) -> None:
    """Сохраняет username бота в настройках Randomizer для реферальных ссылок."""
    from app.db.session import session_scope
    from app.services import randomizer_settings as rset

    try:
        async with session_scope() as session:
            existing = await rset.get_bot_username(session)
            if existing:
                return
            if settings.bot_username:
                await rset.set_bot_username(session, settings.bot_username)
                logger.info("Username бота взят из настроек: @%s", settings.bot_username)
                return
            me = await bot.get_me()
            if me and me.username:
                await rset.set_bot_username(session, me.username)
                logger.info("Username бота определён через Telegram API: @%s", me.username)
            else:
                logger.error("Не удалось определить username бота — реферальные ссылки недоступны.")
    except Exception as e:  # noqa: BLE001
        logger.error("Ошибка автоопределения username бота: %s", e)


async def _maybe_start_api(settings):
    """Запускает серверный API Mini App, если установлен uvicorn."""
    try:
        import uvicorn

        from app.api import app as api_app
    except Exception as e:  # noqa: BLE001
        logger.warning("API-сервер Mini App не запущен (uvicorn/FastAPI недоступны): %s", e)
        return None

    config = uvicorn.Config(
        api_app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    logger.info("API Fable Randomizer запущен на %s:%s", settings.api_host, settings.api_port)
    return server


if __name__ == "__main__":
    asyncio.run(main())
