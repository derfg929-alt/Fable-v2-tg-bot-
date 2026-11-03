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
from app.db.models import Chat, Player
from sqlalchemy import select
import random
from aiogram import Bot

logger = logging.getLogger(__name__)

ENGAGEMENT_MESSAGES = [
    "🎣 <i>«Рыба сама себя не поймает... Пора закинуть удочку!»</i>\n👉 Напишите <code>рыбалка</code> или <code>охота</code>!",
    "💰 <i>«Ваш бизнес ждет хозяина! Прибыль накапливается максимум 4 часа!»</i>\n👉 Заберите монеты: <code>бизнес собрать</code>",
    "⛏ <i>«Майнинг-ферма трудится на полную мощность!»</i>\n👉 Заберите намайненную крипту: <code>ферма собрать</code>",
    "🎁 <i>«Не забудьте забрать свой ежедневный бонус и сохранить серию входов!»</i>\n👉 Получить: <code>бонус</code>",
    "📈 <i>«На бирже новое движение курсов крипты и акций! Время инвестировать!»</i>\n👉 Напишите <code>крипта</code> или <code>фондовый рынок</code>",
    "⚔️ <i>«Кто здесь самый меткий стрелок?»</i>\n👉 Бросьте вызов другу: <code>дуэль @user 100</code>!",
    "🤖 <i>«Есть сложный вопрос или нерешенный пример?»</i>\n👉 Спросите меня: <code>фабле сколько будет 25 * 48</code>!",
]


async def background_engagement_worker(bot: Bot, interval_seconds: int = 14400) -> None:
    """Периодически отправляет напоминания в активные чаты, чтобы вовлекать игроков."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with session_scope() as session:
                chats = (
                    await session.scalars(
                        select(Chat).where(Chat.is_active.is_(True))
                    )
                ).all()

                for chat in chats:
                    try:
                        text = random.choice(ENGAGEMENT_MESSAGES)
                        await bot.send_message(chat_id=chat.telegram_id, text=text, parse_mode="HTML")
                        await asyncio.sleep(1)  # избегаем rate-limit
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Ошибка в фоновом вовлечении: {e}")


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
