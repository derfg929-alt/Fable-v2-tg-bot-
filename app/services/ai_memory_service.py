"""Память диалога ИИ Fable.

Хранит историю общения пользователя с ИИ отдельно для каждого чата
(модель AIConversation, таблица ai_conversations). Это позволяет ИИ
«поднимать» контекст беседы: помнить, о чём говорили, и отвечать по теме.

История передаётся внешней модели как chat_history и офлайн-движку как
компактная текстовая выдержка. Окно истории ограничено, чтобы не раздувать
запросы и не терять релевантный контекст.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AIConversation
from app.services.player_service import get_or_create_player

# Максимум сообщений (user+assistant) в активном окне памяти: 12 обменов.
MAX_HISTORY_MESSAGES = 24
# Сколько сообщений уходит во внешнюю модель (последние обмены).
MODEL_HISTORY_MESSAGES = 12
# Сколько символов офлайн-контекста показываем движку.
MAX_CONTEXT_CHARS = 1500

_TRIGGER_RE = re.compile(r"\b(фабле|fable|фабля|фаблик|грок|grok)\b", re.IGNORECASE)
_FORGET_RE = re.compile(r"\b(забудь|забудь всё|забудь все|очисти память|сбрось память|forget)\b", re.IGNORECASE)


def is_forget_request(text: str) -> bool:
    """Определяет, просит ли пользователь забыть историю диалога."""
    return bool(text and _FORGET_RE.search(text))


def _clean_for_history(text: str) -> str:
    cleaned = _TRIGGER_RE.sub("", text or "").strip(" ,:;!?\n")
    return (cleaned or (text or "").strip())[:1000]


async def _get_or_create(session: AsyncSession, *, user_id: int, chat_id: int) -> AIConversation:
    conv = await session.scalar(
        select(AIConversation).where(
            AIConversation.user_id == user_id,
            AIConversation.chat_id == chat_id,
        )
    )
    if conv:
        return conv
    await get_or_create_player(session, telegram_id=user_id, first_name="Игрок")
    conv = AIConversation(user_id=user_id, chat_id=chat_id, history_json=[], message_count=0)
    session.add(conv)
    await session.flush()
    return conv


async def load_history(
    session: AsyncSession, *, user_id: int, chat_id: int, for_model: bool = True
) -> list[dict[str, str]]:
    """Возвращает историю диалога в формате chat_history для модели."""
    conv = await session.scalar(
        select(AIConversation).where(
            AIConversation.user_id == user_id,
            AIConversation.chat_id == chat_id,
        )
    )
    if not conv or not conv.history_json:
        return []
    history = [m for m in conv.history_json if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if for_model:
        return history[-MODEL_HISTORY_MESSAGES:]
    return history


async def build_context_string(session: AsyncSession, *, user_id: int, chat_id: int) -> str:
    """Собирает компактную выдержку диалога для офлайн-движка Fable."""
    history = await load_history(session, user_id=user_id, chat_id=chat_id, for_model=False)
    if not history:
        return ""
    lines: list[str] = []
    for msg in history[-8:]:
        speaker = "Игрок" if msg.get("role") == "user" else "Fable"
        content = str(msg.get("content", "")).strip().replace("\n", " ")
        if content:
            lines.append(f"{speaker}: {content}")
    context = "\n".join(lines)
    return context[-MAX_CONTEXT_CHARS:]


async def remember_exchange(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    user_text: str,
    assistant_text: str,
) -> AIConversation:
    """Сохраняет обмен репликами в память диалога, обрезая окно истории."""
    conv = await _get_or_create(session, user_id=user_id, chat_id=chat_id)
    history = [m for m in (conv.history_json or []) if isinstance(m, dict)]

    user_clean = _clean_for_history(user_text)
    if user_clean:
        history.append({"role": "user", "content": user_clean})
    assistant_clean = (assistant_text or "").strip()[:2000]
    if assistant_clean:
        history.append({"role": "assistant", "content": assistant_clean})

    if len(history) > MAX_HISTORY_MESSAGES:
        history = history[-MAX_HISTORY_MESSAGES:]

    conv.history_json = history
    conv.message_count = (conv.message_count or 0) + (1 if user_clean else 0)
    if user_clean:
        conv.topic = user_clean[:255]
        conv.summary = _build_summary(history)
    await session.flush()
    return conv


async def clear_history(session: AsyncSession, *, user_id: int, chat_id: int) -> bool:
    """Полностью очищает память диалога пользователя в чате."""
    conv = await session.scalar(
        select(AIConversation).where(
            AIConversation.user_id == user_id,
            AIConversation.chat_id == chat_id,
        )
    )
    if not conv:
        return False
    conv.history_json = []
    conv.topic = None
    conv.summary = None
    conv.message_count = 0
    await session.flush()
    return True


def _build_summary(history: list[dict]) -> str:
    """Короткая сводка последних реплик игрока для отслеживания темы."""
    user_lines = [str(m.get("content", "")) for m in history if m.get("role") == "user"]
    tail = " | ".join(line.strip() for line in user_lines[-3:] if line.strip())
    return tail[:500]
