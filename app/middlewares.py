"""Middlewares бота Fable:
1. MessageActivityAndSecurityMiddleware:
   - Автоматически начисляет кристаллы и опыт за общение в группах (ТЗ 2.1).
   - Проверяет сообщения на запрещенные ссылки в чатах с ограничением ссылок (ТЗ 7.1).
   - Проверяет статус заморозки чата (ТЗ 7.9).
   - Проверяет обязательное принятие правил (ТЗ 9.1).
   - Не накладывает автоматических мутов/банов, строго удаляет сообщение при нарушении правил ссылок.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, types
from aiogram.enums import ChatType

from app.db.session import session_scope
from sqlalchemy import select
from app.db.models import ModerationCase
from app.services.activity_service import reward_chat_message
from app.services.moderation_service import (
    apply_warn_escalation,
    get_user_admin_level,
    is_link_allowed,
    issue_mute,
    issue_warn,
)
from app.services.badwords_service import check_message
from app.services.chat_settings_service import (
    get_or_create_chat,
    has_accepted_rules,
    has_pending_captcha,
)
from app.services.antispam_service import check_and_apply_antispam, check_flood_pause


class CallbackAnswerMiddleware(BaseMiddleware):
    """Гарантирует вызов callback.answer() для любых inline-кнопок,
    предотвращая вечную загрузку ("часики") даже при необработанных или упавших событиях.
    """
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, types.CallbackQuery):
            return await handler(event, data)

        try:
            return await handler(event, data)
        finally:
            try:
                await event.answer()
            except Exception:
                pass


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

                # 3. Проверка обязательной капчи при входе (по умолчанию выключена)
                if settings.get("captcha_enabled", False) and admin_lvl < 2:
                    if await has_pending_captcha(
                        session, chat_id=message.chat.id, user_id=message.from_user.id
                    ):
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        return

                # 4. Проверка антиссылок (по умолчанию выключена; упоминания @user всегда разрешены)
                if settings.get("links_restricted", False) and message.text:
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

                # 5. Автоматический антиспам (по умолчанию выключен)
                if settings.get("antispam_enabled", False) and admin_lvl < 2:
                    try:
                        antispam = await check_and_apply_antispam(
                            session,
                            bot=message.bot,
                            chat_id=message.chat.id,
                            user_id=message.from_user.id,
                            text=message.text or message.caption,
                            settings=settings,
                        )
                    except Exception:
                        antispam = None
                    if antispam is not None:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        return

                # 6. Антимат (фильтр нецензурной лексики; по умолчанию выключен)
                if settings.get("badwords_enabled", False) and admin_lvl < 2:
                    try:
                        hit = await check_message(
                            session,
                            chat_id=message.chat.id,
                            text=message.text or message.caption,
                            settings=settings,
                        )
                    except Exception:
                        hit = None
                    if hit is not None:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        if hit.action == "warn":
                            try:
                                case, cnt = await issue_warn(
                                    session,
                                    chat_id=message.chat.id,
                                    moderator_id=message.bot.id if message.bot else 0,
                                    target_id=message.from_user.id,
                                    reason="Антимат: нецензурная лексика",
                                    bot=message.bot,
                                    system=True,
                                )
                                chat_now = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
                                await apply_warn_escalation(
                                    session,
                                    chat_id=message.chat.id,
                                    target_id=message.from_user.id,
                                    warn_count=cnt,
                                    settings=chat_now.settings or {},
                                    bot=message.bot,
                                )
                            except Exception:
                                pass
                            try:
                                await message.answer(
                                    f"⚠️ {message.from_user.first_name}, нецензурная лексика запрещена! Выдано предупреждение.",
                                )
                            except Exception:
                                pass
                        elif hit.action == "mute":
                            try:
                                await issue_mute(
                                    session,
                                    chat_id=message.chat.id,
                                    moderator_id=message.bot.id if message.bot else 0,
                                    target_id=message.from_user.id,
                                    duration=timedelta(seconds=max(30, hit.duration_seconds)),
                                    reason="Антимат: нецензурная лексика",
                                    bot=message.bot,
                                    system=True,
                                )
                                from aiogram.types import ChatPermissions
                                if message.bot:
                                    await message.bot.restrict_chat_member(
                                        chat_id=message.chat.id,
                                        user_id=message.from_user.id,
                                        permissions=ChatPermissions(can_send_messages=False),
                                    )
                            except Exception:
                                pass
                        return

                # 7. Анти-флуд «пауза»: минимальный интервал между сообщениями игрока
                pause = int(settings.get("antiflood_pause_seconds", 0) or 0)
                if pause > 0 and admin_lvl < 2:
                    try:
                        blocked = await check_flood_pause(
                            session,
                            chat_id=message.chat.id,
                            user_id=message.from_user.id,
                            min_interval_seconds=pause,
                        )
                    except Exception:
                        blocked = False
                    if blocked:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        return

                # 8. Начисление активности за общение
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
