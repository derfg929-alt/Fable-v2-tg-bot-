"""Фоновые задачи бота Fable:
1. Суточный расчет процентов банка.
2. Цикл маркетмейкера биржи.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from app.db.session import session_scope
from app.services.bank_service import accrue_daily_interest
from app.services.market_maker import run_market_maker_cycle

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
