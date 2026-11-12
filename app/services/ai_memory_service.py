"""Память ИИ Fable: сохранение и загрузка контекста диалога.

Бот помнит предыдущие сообщения пользователя и его ответы, чтобы отвечать
связно на уточняющие вопросы («а подробнее?», «почему?», «а про это?»).
История ведётся отдельно для личных сообщений и для каждого группового чата,
чтобы диалоги в разных чатах не смешивались.

Модуль не управляет транзакцией сам: commit/rollback выполняет вызывающий слой.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIConversation

logger = logging.getLogger("fable.ai")

# Сколько последних сообщений (пар user/assistant) хранить и отправлять модели.
MAX_STORED_MESSAGES = 40
MAX_CONTEXT_MESSAGES = 12
MAX_MESSAGE_CHARS = 2000


def _normalize_chat_id(chat_id: int | None) -> int:
    return int(chat_id) if chat_id else 0


async def _get_or_create_row(
    session: AsyncSession, *, user_id: int, chat_id: int
) -> AIConversation:
    row = await session.scalar(
        select(AIConversation).where(
            AIConversation.user_id == user_id,
            AIConversation.chat_id == chat_id,
        )
    )
    if row is None:
        row = AIConversation(user_id=user_id, chat_id=chat_id, history=[])
        session.add(row)
        await session.flush()
    elif not isinstance(row.history, list):
        row.history = []
    return row


async def get_conversation_history(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int | None = None,
    limit: int = MAX_CONTEXT_MESSAGES,
) -> list[dict[str, str]]:
    """Возвращает последние сообщения диалога в формате OpenAI (role/content)."""
    if user_id <= 0:
        return []
    try:
        row = await _get_or_create_row(session, user_id=user_id, chat_id=_normalize_chat_id(chat_id))
    except Exception as err:  # память не должна ломать основной ответ
        logger.debug("Не удалось загрузить память ИИ: %s", err)
        return []

    history = row.history or []
    cleaned: list[dict[str, str]] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": str(content)})
    return cleaned[-limit:]


async def remember_exchange(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int | None,
    user_message: str,
    assistant_message: str,
) -> None:
    """Сохраняет пару «вопрос пользователя — ответ ИИ» в память диалога."""
    if user_id <= 0:
        return
    user_text = (user_message or "").strip()[:MAX_MESSAGE_CHARS]
    assistant_text = (assistant_message or "").strip()[:MAX_MESSAGE_CHARS]
    if not user_text or not assistant_text:
        return

    try:
        row = await _get_or_create_row(session, user_id=user_id, chat_id=_normalize_chat_id(chat_id))
    except Exception as err:
        logger.debug("Не удалось сохранить память ИИ: %s", err)
        return

    history = list(row.history or [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    # Храним только последние сообщения, чтобы JSON не разрастался бесконечно.
    row.history = history[-MAX_STORED_MESSAGES:]
    await session.flush()


async def clear_conversation(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int | None = None,
) -> bool:
    """Сбрасывает память диалога. Возвращает True, если история существовала."""
    if user_id <= 0:
        return False
    row = await session.scalar(
        select(AIConversation).where(
            AIConversation.user_id == user_id,
            AIConversation.chat_id == _normalize_chat_id(chat_id),
        )
    )
    if row is None:
        return False
    row.history = []
    await session.flush()
    return True
