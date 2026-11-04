"""Сервис вовлечения и напоминаний (engagement / bait).

Функции:
1. Отправка напоминаний/байтов игрокам в ЛС:
   - Бизнес переполнен (прошло >= 4 часов, касса полная).
   - Ежедневный бонус доступен для сбора.
   - Крипто-ферма накопила добычу.
2. Интерактивные байты в групповых чатах:
   - Если в чате затишье, отправляется интерактивный bait: дроп монеток (кнопка 'Забрать'),
     мини-опрос или напоминание об активности, чтобы чат оживал и участники взаимодействовали с ботом.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone, timedelta
from typing import Any
from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerBusiness, DailyBonus, Chat, PlayerMining
from app.services.economy_service import credit_coins

logger = logging.getLogger(__name__)

CHAT_BAIT_MESSAGES = [
    "🔥 <i>Что-то в чате стало тихо... Кто первым нажмет на кнопку, тот получит монеты!</i>",
    "💎 <i>Сундук с сокровищами материализовался в чате! Забирай скорее, пока не утащили!</i>",
    "🎲 <i>Время активности! Нажмите кнопку, чтобы получить порцию монет от Fable!</i>",
    "⚡️ <i>Случайный денежный дроп для активных участников!</i>",
]

DM_BAIT_MESSAGES = [
    "👋 Привет! Твой ежедневный бонус уже ждет тебя в Fable! Заходи забрать: напиши <code>бонус</code>!",
    "💼 Внимание! Твои бизнесы уже накопили максимальную прибыль за 4 часа и простаивают! Напиши <code>бизнес собрать</code>, чтобы забрать кассу!",
    "⛏ Твоя майнинг-ферма намайнила новую крипту! Загляни проверить: напиши <code>ферма собрать</code> или <code>портфель</code>!",
    "📈 На фондовом рынке обновились котировки! Проверь курс: напиши <code>фондовый рынок</code>!",
]


async def generate_chat_bait_message() -> tuple[str, InlineKeyboardMarkup]:
    """Генерирует сообщение-байт для чата с кнопкой быстрого сбора."""
    text = random.choice(CHAT_BAIT_MESSAGES)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 Забрать монеты!",
                    callback_data="bait:claim",
                )
            ]
        ]
    )
    return text, kb


async def process_dm_baits(session: AsyncSession, bot: Bot) -> int:
    """Проверяет игроков и отправляет байт-напоминания в ЛС тем, кто давно не заходил."""
    now = datetime.now(timezone.utc)
    sent_count = 0

    # 1. Проверяем игроков с переполненным бизнесом (>4 часов)
    four_hours_ago = now - timedelta(hours=4)
    businesses = await session.execute(
        select(PlayerBusiness.user_id)
        .where(PlayerBusiness.last_collected_at <= four_hours_ago)
        .group_by(PlayerBusiness.user_id)
        .limit(20)
    )
    for user_id in businesses.scalars():
        try:
            await bot.send_message(
                chat_id=user_id,
                text="💼 <b>Ваши бизнесы переполнены!</b>\n\nПрошло более 4 часов, касса заполнена и доход больше не начисляется!\nНапишите <code>бизнес собрать</code>, чтобы забрать прибыль и продолжить работу!",
                parse_mode="HTML",
            )
            sent_count += 1
        except Exception as e:
            logger.debug(f"Не удалось отправить bait в ЛС {user_id}: {e}")

    # 2. Проверяем ежедневный бонус (>24 часов)
    twenty_four_hours_ago = now - timedelta(hours=24)
    bonus_players = await session.execute(
        select(DailyBonus.user_id)
        .where(DailyBonus.last_claimed_at <= twenty_four_hours_ago)
        .limit(20)
    )
    for user_id in bonus_players.scalars():
        try:
            await bot.send_message(
                chat_id=user_id,
                text="🎁 <b>Ежедневный бонус готов к получению!</b>\n\nНе потеряйте серию входов! Напишите <code>бонус</code>, чтобы забрать монеты и опыт!",
                parse_mode="HTML",
            )
            sent_count += 1
        except Exception as e:
            logger.debug(f"Не удалось отправить бонус-байт в ЛС {user_id}: {e}")

    return sent_count


async def process_chat_baits(session: AsyncSession, bot: Bot) -> int:
    """Отправляет интерактивные байты в случайные активные групповые чаты."""
    # Случайный порядок чатов, чтобы байты распределялись по разным чатам
    chats = await session.scalars(
        select(Chat)
        .where(Chat.is_active == True, Chat.chat_type.in_(["group", "supergroup"]))
        .order_by(func.random())
        .limit(3)
    )
    sent_count = 0
    for chat in chats.all():
        try:
            text, kb = await generate_chat_bait_message()
            await bot.send_message(
                chat_id=chat.telegram_id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
            )
            sent_count += 1
        except Exception as e:
            logger.debug(f"Не удалось отправить байт в чат {chat.telegram_id}: {e}")

    return sent_count
