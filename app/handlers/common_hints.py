"""Общие подсказки для команд, которые работают только в группе или у владельца.

Без них такие команды в личных сообщениях молча игнорировались: пользователь
писал «настройки» или «админы», не получал ничего и считал бота сломанным.
"""
from __future__ import annotations

from aiogram import types

GROUP_ONLY_HINT = (
    "ℹ️ Команда <code>{cmd}</code> работает только в группе.\n\n"
    "Добавь меня в чат и напиши её там — тогда я покажу настройки и модерацию этого чата."
)

OWNER_ONLY_HINT = (
    "🔒 Команда <code>{cmd}</code> доступна только владельцу бота в личных сообщениях."
)


async def reply_group_only(message: types.Message, cmd: str | None = None) -> None:
    """Отвечает, что команда доступна только в группе."""
    command = cmd or (message.text or "").split()[0]
    try:
        await message.reply(GROUP_ONLY_HINT.format(cmd=command), parse_mode="HTML")
    except Exception:
        pass


async def reply_owner_only(message: types.Message, cmd: str | None = None) -> None:
    """Отвечает, что команда доступна только владельцу бота."""
    command = cmd or (message.text or "").split()[0]
    try:
        await message.reply(OWNER_ONLY_HINT.format(cmd=command), parse_mode="HTML")
    except Exception:
        pass
