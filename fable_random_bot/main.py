"""Точка входа бота розыгрышей (aiogram 3, polling, без FastAPI/Uvicorn)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import config, database
from .handlers import admin as admin_handlers
from .handlers import draw as draw_handlers
from .handlers import start as start_handlers
from .handlers import user as user_handlers
from .utils import SubscriptionChecker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fable_random_bot")


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Порядок важен: admin-роутер должен обрабатывать FSM-текст раньше user.
    dp.include_router(start_handlers.router)
    dp.include_router(admin_handlers.router)
    dp.include_router(user_handlers.router)
    dp.include_router(draw_handlers.router)
    return dp


async def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Укажи переменную окружения BOT_TOKEN.")

    # Создаём файл базы сразу при запуске, чтобы он гарантированно существовал.
    await database.load_data()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = create_dispatcher()

    checker = SubscriptionChecker(bot)
    checker.start()
    user_handlers.checker = checker

    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        draw_handlers.check_and_finish, "interval", minutes=1, args=[bot], id="finish"
    )
    scheduler.add_job(
        draw_handlers.check_reserve_timeouts, "interval", minutes=5, args=[bot], id="reserve"
    )
    scheduler.start()
    logger.info("Планировщик запущен. База: %s", config.DB_PATH)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await checker.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        logger.info("Остановлено: %s", exc)
