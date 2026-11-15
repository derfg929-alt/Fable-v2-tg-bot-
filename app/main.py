"""Главная точка входа Telegram-бота Fable (aiogram 3)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
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

    # Mini App и верификатор: регистрируем кнопки меню, если известен публичный адрес.
    await _setup_bot_menu(bot, settings)

    # Серверный API и Mini App запускается ДО polling: если Telegram недоступен,
    # HTTP-сервер продолжает работать, и Mini App остаётся доступным.
    api = await _maybe_start_api(settings)
    api_server, api_task = api if api else (None, None)

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

    logger.info("Запуск Telegram-бота Fable в режиме Long Polling...")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(sig_name: str) -> None:
        logger.info("Получен сигнал %s — останавливаем бота…", sig_name)
        stop_event.set()
        # Если polling сейчас активен, мягко останавливаем его; иначе цикл
        # _poll_forever сам завершится по stop_event (например, во время паузы).
        if dp._running_lock.locked():
            asyncio.create_task(dp.stop_polling())

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop, sig.name)

    async def _poll_forever() -> None:
        # Polling перезапускается при ошибках (например, Telegram недоступен или
        # временно отклонил запросы), чтобы не ронять процесс: HTTP-сервер Mini App
        # должен продолжать работать даже если Telegram-соединение потеряно.
        while not stop_event.is_set():
            try:
                # handle_signals=False: сигналы обрабатываем сами выше, иначе
                # aiogram перехватывает их и «глотает» в паузах между рестартами.
                await dp.start_polling(
                    bot, polling_timeout=settings.polling_timeout, handle_signals=False
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Polling остановился: %s. Повторная попытка через 15 секунд "
                    "(Mini App продолжает работать).", e,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=15)

    try:
        await _poll_forever()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
        for task in (bank_task, mm_task, engagement_task, moderation_expiry_task, randomizer_task):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(
                bank_task, mm_task, engagement_task, moderation_expiry_task, randomizer_task,
                return_exceptions=True,
            )
        if api_server is not None:
            api_server.should_exit = True
        if api_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(api_task, timeout=10)
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


async def _setup_bot_menu(bot, settings) -> None:
    """Регистрирует кнопки меню бота: Mini App и верификатор честности."""
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    base = settings.effective_mini_app_url
    if not base:
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🎁 Конкурсы", web_app=WebAppInfo(url=base))
        )
        logger.info("Кнопка меню Mini App установлена: %s", base)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось установить кнопку меню Mini App: %s", e)


async def _maybe_start_api(settings):
    """Запускает серверный API Mini App, если установлен uvicorn."""
    try:
        import uvicorn

        from app.api import app as api_app
    except Exception as e:  # noqa: BLE001
        logger.warning("API-сервер Mini App не запущен (uvicorn/FastAPI недоступны): %s", e)
        return None

    port = settings.effective_api_port
    config = uvicorn.Config(
        api_app,
        host=settings.api_host,
        port=port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    # Uvicorn по умолчанию перехватывает SIGTERM/SIGINT и после остановки заново
    # поднимает полученный сигнал — это конфликтует с обработчиком aiogram и
    # приводит к «Получен сигнал SIGTERM» и аварийному завершению процесса.
    # Отключаем захват сигналов: сервер останавливается только через should_exit.
    server.capture_signals = contextlib.nullcontext
    task = asyncio.create_task(server.serve())

    def _log_api_failure(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("API-сервер Mini App остановлен с ошибкой: %s", exc)

    task.add_done_callback(_log_api_failure)
    logger.info("API Fable Randomizer запущен на %s:%s", settings.api_host, port)
    return server, task


if __name__ == "__main__":
    asyncio.run(main())
