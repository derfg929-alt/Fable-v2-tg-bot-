"""Фоновые задачи бота Fable:
1. Суточный расчет процентов банка.
2. Цикл маркетмейкера биржи.
3. Фоновый воркер вовлечения и байтов (в ЛС и в чаты).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import random
from aiogram import Bot

from app.db.session import session_scope
from app.services.bank_service import accrue_daily_interest
from app.services.market_maker import run_market_maker_cycle
from app.services.engagement_service import process_dm_baits, process_chat_baits

logger = logging.getLogger(__name__)


async def background_bank_interest_worker(interval_seconds: int = 3600) -> None:
    """Периодически проверяет и начисляет суточные проценты банка."""
    while True:
        try:
            async with session_scope() as session:
                results = await accrue_daily_interest(session)
                applied_count = sum(1 for r in results if r.applied)
                total_coins = sum(r.interest for r in results if r.applied)
                if applied_count > 0:
                    logger.info(
                        f"Банковские проценты начислены: "
                        f"обработано {len(results)} счетов, начислено {total_coins} монет {applied_count} игрокам"
                    )
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче банка: {e}")
        await asyncio.sleep(interval_seconds)


async def background_market_maker_worker(interval_seconds: int = 30) -> None:
    """Периодически обновляет котировки и поддерживает ликвидность маркетмейкера."""
    while True:
        try:
            async with session_scope() as session:
                await run_market_maker_cycle(session)
        except Exception as e:
            logger.error(f"Ошибка в фоновом цикле маркетмейкера: {e}")
        await asyncio.sleep(interval_seconds)


async def background_engagement_worker(
    bot: Bot,
    interval_seconds: int | None = None,
    min_interval_seconds: int = 10800,
    max_interval_seconds: int = 21600,
) -> None:
    """Периодически (раз в 3-6 часов) отправляет интерактивные байты в случайные чаты и напоминания в ЛС."""
    while True:
        sleep_duration = (
            interval_seconds
            if interval_seconds is not None
            else random.randint(min_interval_seconds, max_interval_seconds)
        )
        try:
            async with session_scope() as session:
                dm_count = await process_dm_baits(session, bot)
                chat_count = await process_chat_baits(session, bot)
                if dm_count > 0 or chat_count > 0:
                    logger.info(
                        f"Engagement worker: отправлено {dm_count} ЛС-напоминаний и {chat_count} байтов в чаты. "
                        f"Следующий запуск через {sleep_duration // 3600} ч {(sleep_duration % 3600) // 60} мин."
                    )
        except Exception as e:
            logger.error(f"Ошибка в воркере вовлечения: {e}")
        await asyncio.sleep(sleep_duration)
