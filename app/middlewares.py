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
from sqlalchemy import select
from app.db.models import ModerationCase, Player
from app.services.activity_service import reward_chat_message
from app.services.moderation_service import (
    is_link_allowed,
    get_user_admin_level,
    has_active_chat_ban,
)
from app.services.chat_settings_service import get_or_create_chat, has_accepted_rules


class CallbackAnswerMiddleware(BaseMiddleware):
    """Гарантирует вызов callback.answer() для любых inline-кнопок,
    предотвращая вечную загрузку ("часики") даже при необработанных или упавших событиях.
    Также блокирует нажатия локально забаненного в чате игрока (бан действует только в этом чате).
    """
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, types.CallbackQuery):
            return await handler(event, data)

        if await self._blocked_by_local_ban(event):
            try:
                await event.message.delete()
            except Exception:
                pass
            try:
                await event.answer("Вы заблокированы в этом чате.", show_alert=True)
            except Exception:
                pass
            return

        try:
            return await handler(event, data)
        finally:
            try:
                await event.answer()
            except Exception:
                pass

    @staticmethod
    async def _blocked_by_local_ban(event: types.CallbackQuery) -> bool:
        try:
            chat = event.message.chat if event.message else None
            if chat is None or chat.type == ChatType.PRIVATE or not event.from_user:
                return False
            async with session_scope() as session:
                lvl = await get_user_admin_level(
                    session, chat_id=chat.id, user_id=event.from_user.id
                )
                if lvl >= 2:
                    return False
                return await has_active_chat_ban(
                    session, chat_id=chat.id, target_id=event.from_user.id
                )
        except Exception:
            return False


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

        # Глобальный бан (ТЗ 13.5): запрещает пользоваться экономикой, биржей
        # и играми во всех чатах. Не является Telegram-баном, не трогает
        # админку чата и НЕ удаляет данные игрока (монеты, бизнесы, фермы).
        # Локальные баны конкретных чатов проверяются отдельно ниже и на
        # другие чаты не распространяются.
        if message.text or message.caption:
            async with session_scope() as session:
                banned = await session.scalar(
                    select(Player.is_globally_banned).where(
                        Player.telegram_id == message.from_user.id
                    )
                )
            if banned:
                if message.chat and message.chat.type == ChatType.PRIVATE:
                    try:
                        await message.reply(
                            "🚫 Ваш аккаунт Fable заблокирован владельцем. "
                            "Экономика, биржа и игры недоступны.",
                        )
                    except Exception:
                        pass
                return

        # Обработка только групповых чатов
        if message.chat and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            async with session_scope() as session:
                now = datetime.now(timezone.utc)
                # 0. Проверка локального активного мута в БД (на случай задержки Bot API)
                active_mute = await session.scalar(
                    select(ModerationCase).where(
                        ModerationCase.chat_id == message.chat.id,
                        ModerationCase.target_id == message.from_user.id,
                        ModerationCase.action == "mute",
                        ModerationCase.is_active.is_(True),
                        ModerationCase.expires_at > now,
                    ).limit(1)
                )
                if active_mute:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    return

                chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
                settings = chat.settings or {}

                # Уровень прав запрашиваем один раз и переиспользуем во всех проверках ниже.
                admin_lvl = await get_user_admin_level(
                    session, chat_id=message.chat.id, user_id=message.from_user.id
                )

                # 0.1. Локальный бан: действует только в этом чате. В других
                # группах игрок сохраняет полный доступ и все свои данные.
                if admin_lvl < 2 and await has_active_chat_ban(
                    session, chat_id=message.chat.id, target_id=message.from_user.id
                ):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    return

                # 1. Проверка заморозки чата
                frozen_until_str = settings.get("frozen_until")
                if frozen_until_str:
                    try:
                        frozen_until = datetime.fromisoformat(frozen_until_str)
                        if datetime.now(timezone.utc) < frozen_until:
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
