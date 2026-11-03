"""Middlewares бота Fable:
1. MessageActivityAndSecurityMiddleware:
   - Автоматически начисляет кристаллы и опыт за общение в группах (ТЗ 2.1).
   - Проверяет сообщения на запрещенные ссылки в чатах с ограничением ссылок (ТЗ 7.1).
   - Проверяет статус заморозки чата (ТЗ 7.9).
   - Проверяет обязательное принятие правил (ТЗ 9.1).
   - Не накладывает автоматических мутов/банов, строго удаляет сообщение при нарушении правил ссылок.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, types
from aiogram.enums import ChatType

from app.db.session import session_scope
from app.services.activity_service import reward_chat_message
from app.services.moderation_service import is_link_allowed, get_user_admin_level
from app.services.chat_settings_service import get_or_create_chat, has_accepted_rules


class MessageActivityAndSecurityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, types.Message):
            return await handler(event, data)

        message: types.Message = event
        if not message.from_user or message.from_user.is_bot:
            return await handler(event, data)

        # Обработка только групповых чатов
        if message.chat and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            async with session_scope() as session:
                chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
                settings = chat.settings or {}

                # 1. Проверка заморозки чата
                frozen_until_str = settings.get("frozen_until")
                if frozen_until_str:
                    try:
                        frozen_until = datetime.fromisoformat(frozen_until_str)
                        if datetime.now(timezone.utc) < frozen_until:
                            admin_lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id)
                            if admin_lvl < 2:
                                try:
                                    await message.delete()
                                except Exception:
                                    pass
                                return
                    except Exception:
                        pass

                # 2. Проверка обязательного принятия правил
                if settings.get("rules_acceptance_required", False):
                    accepted = await has_accepted_rules(session, chat_id=message.chat.id, user_id=message.from_user.id)
                    admin_lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id)
                    if not accepted and admin_lvl < 2:
                        try:
                            await message.delete()
                            await message.answer(
                                f"⚠️ {message.from_user.first_name}, перед отправкой сообщений необходимо принять правила чата (команда <code>правила</code>)!",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                        return

                # 3. Проверка антиссылок
                if settings.get("links_restricted", True) and message.text:
                    admin_lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id)
                    if admin_lvl < 2:
                        allowed, matched_url = await is_link_allowed(session, chat_id=message.chat.id, text=message.text)
                        if not allowed:
                            try:
                                await message.delete()
                                await message.answer(
                                    f"⚠️ {message.from_user.first_name}, отправка ссылок в этом чате запрещена!",
                                )
                            except Exception:
                                pass
                            return

                # 4. Начисление активности за общение
                if message.text:
                    try:
                        await reward_chat_message(
                            session,
                            chat_id=message.chat.id,
                            user_id=message.from_user.id,
                            message_id=message.message_id,
                            text=message.text,
                        )
                    except Exception:
                        pass

        return await handler(event, data)
