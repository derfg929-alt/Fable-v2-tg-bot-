"""Сервис логирования важных административных действий Fable.

Правила из ТЗ:
- Запись всех важных действий (мут, размут, варн, снятие варна, бан, разбан, кик, очистка, заморозка, смена правил, сброс экономики).
- Логирование в таблицу AdminActionLog.
- При наличии настроенного log_channel_id — отправка структурированного уведомления в Telegram log-канал.
- Ошибки не должны отправляться владельцу в ЛС, а пишутся в технический лог.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminActionLog, Chat

logger = logging.getLogger(__name__)


async def log_admin_action(
    session: AsyncSession,
    *,
    bot: Bot | None = None,
    chat_id: int | None,
    actor_id: int | None,
    target_id: int | None,
    action: str,
    reason: str | None = None,
    details: dict | None = None,
) -> AdminActionLog:
    """Записывает действие в БД и отправляет в лог-канал при его наличии."""
    entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=actor_id,
        target_id=target_id,
        action=action,
        reason=reason,
        details=details or {},
    )
    session.add(entry)
    await session.flush()

    if bot and chat_id is not None:
        try:
            chat = await session.get(Chat, chat_id)
            log_channel_id = chat.log_chat_id if chat else None
            if not log_channel_id and chat and chat.settings:
                log_channel_id = chat.settings.get("log_channel_id")

            if log_channel_id:
                msg = (
                    f"🛡 [АДМИН-ЛОГ] Действие: <b>{action}</b>\n"
                    f"Чат: <code>{chat_id}</code>\n"
                    f"Инициатор: <code>{actor_id}</code>\n"
                    + (f"Цель: <code>{target_id}</code>\n" if target_id else "")
                    + (f"Причина: {reason}\n" if reason else "")
                )
                await bot.send_message(chat_id=log_channel_id, text=msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить лог в канал: {e}")

    return entry
