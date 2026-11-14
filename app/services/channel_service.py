"""Сервис каналов Fable Randomizer.

Отвечает за разрешение каналов (по @username, ссылке или ID), проверку прав
бота и — главное для пользовательских конкурсов — проверку того, что
конкретный пользователь действительно является владельцем/администратором
канала, за который он создаёт конкурс.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel

logger = logging.getLogger("fable.channel")

_ADMIN_STATUSES = ("creator", "administrator")


def normalize_channel_token(token: str) -> str:
    """Приводит ввод пользователя к @username или числовому ID канала."""
    raw = (token or "").strip()
    if not raw:
        return ""
    if raw.lstrip("-").isdigit():
        return raw
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "telegram.me/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.split("?", 1)[0].split("/", 1)[0]
    return raw if raw.startswith("@") else f"@{raw}"


async def resolve_channel(bot: Bot, token: str):
    """Возвращает объект chat из Telegram API или None, если канал не найден."""
    normalized = normalize_channel_token(token)
    if not normalized:
        return None
    try:
        if normalized.lstrip("-").isdigit():
            return await bot.get_chat(int(normalized))
        return await bot.get_chat(normalized)
    except Exception as e:  # noqa: BLE001 — канал недоступен/бот не участник
        logger.warning("Не удалось получить канал %s: %s", token, e)
        return None


async def ensure_channel(
    session: AsyncSession, bot: Bot, token: str, *, added_by: int | None = None
) -> Channel | None:
    """Находит или создаёт запись канала в БД по данным Telegram API."""
    chat = await resolve_channel(bot, token)
    if chat is None:
        return None

    existing = await session.scalar(select(Channel).where(Channel.chat_id == chat.id))
    if existing is not None:
        existing.title = chat.title or existing.title
        existing.username = chat.username or existing.username
        existing.is_active = True
        await session.flush()
        return existing

    channel = Channel(
        chat_id=chat.id,
        username=chat.username,
        title=chat.title,
        invite_link=getattr(chat, "invite_link", None),
        is_active=True,
        added_by=added_by,
    )
    session.add(channel)
    await session.flush()
    return channel


async def get_user_channel_role(bot: Bot, chat_id: int, user_id: int) -> str | None:
    """Возвращает статус пользователя в канале (creator/administrator/member/...)."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось проверить роль пользователя %s в канале %s: %s", user_id, chat_id, e)
        return None
    return getattr(member, "status", None)


async def is_channel_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """True, если пользователь — владелец или администратор канала."""
    role = await get_user_channel_role(bot, chat_id, user_id)
    return role in _ADMIN_STATUSES


async def check_bot_rights(bot: Bot, chat_id: int) -> str | None:
    """Возвращает предупреждение, если у бота нет прав в канале, иначе None."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        status = getattr(member, "status", None)
        if status not in _ADMIN_STATUSES:
            return "⚠️ Бот не является администратором этого канала — проверка подписок может не работать."
        if status == "administrator" and not getattr(member, "can_restrict_members", True):
            return "⚠️ У бота нет прав для полной проверки подписчиков канала."
        return None
    except Exception as e:  # noqa: BLE001
        return f"⚠️ Не удалось проверить права бота в канале: {e}"


async def get_channel_by_chat_id(session: AsyncSession, chat_id: int) -> Channel | None:
    return await session.scalar(select(Channel).where(Channel.chat_id == chat_id))


async def list_user_channels(session: AsyncSession, user_id: int) -> list[Channel]:
    """Каналы, которыми управляет пользователь (закреплены за ним)."""
    rows = await session.scalars(
        select(Channel).where(Channel.owner_id == user_id, Channel.is_active.is_(True)).order_by(Channel.id.asc())
    )
    return list(rows.all())
