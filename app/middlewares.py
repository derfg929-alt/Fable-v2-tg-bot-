"""Middlewares бота Fable:
1. MessageActivityAndSecurityMiddleware:
   - Автоматически начисляет монеты за общение в группах (ТЗ 2.1).
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
from app.services.activity_service import record_message_activity, reward_chat_message
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
from app.services.ai_moderation_service import classify_message, looks_like_bot_name

# Команды и кнопки главного меню, которые должны работать всегда — даже если
# пользователь в этот момент находится в пошаговом мастере (FSM). Иначе хэндлеры
# состояний (StateFilter) перехватывают ввод и команды вида «работа» молча
# игнорируются, потому что роутер с мастером зарегистрирован раньше.
COMMAND_ESCAPE_TEXTS = frozenset({
    # Главное меню Randomizer (текст кнопок reply-клавиатуры).
    "🎁 конкурсы", "📣 мои конкурсы", "➕ создать конкурс", "👤 мой профиль",
    "👥 пригласить друзей", "🏆 победители", "📜 история участия", "ℹ️ помощь",
    "конкурсы", "мои конкурсы", "создать конкурс", "новый конкурс", "мой профиль",
    "пригласить друзей", "победители", "история участия", "помощь", "команды", "старт",
    # Экономика и профиль.
    "профиль", "баланс", "банк", "инвентарь", "магазин", "машины", "авто",
    "автосалон", "гараж", "мои авто", "мои машины", "недвижимость", "моя недвижимость",
    "бизнес", "кейсы", "кейс", "ферма", "майнинг", "купить ферму", "космодром",
    "снаряжение", "экипировка", "история", "рейтинг", "купить рейтинг",
    "продать рейтинг", "работа", "подработка", "реп", "реф", "промокоды", "промо",
    "портфель", "рынок", "биржа", "фондовый рынок", "промо список", "промо лист",
    # Игры и развлечения.
    "игры", "казино", "бонус", "ежедневный бонус", "рыбалка", "охота", "кости",
    "монетка", "слоты", "рулетка", "сундук", "краш", "тир", "дуэль", "шар", "реши",
    "брак", "развод", "ник", "обнять", "обнять всех", "поцеловать", "погладить",
    "ударить", "дать пять", "тык", "шлёпнуть", "укусить", "комплимент", "кубик", "куб",
    "roll", "бросок", "шип", "ship", "совместимость", "правда действие",
    "правда или действие", "пди", "кто я", "ктоя", "рандом", "выбор", "пинг",
    # Топы и кланы.
    "топ", "топ богатых", "топ активных", "топ кланов", "клан", "клан создать",
    # Модерация и настройки чата.
    "мут", "размут", "варн", "варны", "преды", "предупреждение", "разварн", "кик",
    "бан", "разбан", "чистка", "очистка", "закрепить", "открепить", "ссылки",
    "антиспам", "антимат", "антифлуд", "настройки", "админка", "админ", "правила",
    "правило", "наказания", "проступки", "казы", "репорт", "логи", "журнал",
    "модерация", "заморозка", "слоумод", "состав", "админы", "назначить",
    "варн настройки", "варн лимит", "варн действие", "варн срок", "варн эскалация",
    # Достижения, квесты и панель владельца.
    "ачивки", "достижения", "квесты", "квест", "понель", "панель",
    "панель управления", "овнер панель", "балансы", "сброс экономики", "выдать",
    "забрать", "инфо", "глобальный бан", "глобальный разбан", "настройки чаты",
    "настройки стоп", "настройки старт",
})


# Первое слово команд, которые принимают аргументы («шахта улучшить»,
# «подработка таксист», «перевод @user 100»). Здесь только глаголы и
# административные действия, которые почти не встречаются как первое слово в
# названии/описании конкурса, — иначе мастер терял бы введённый пользователем
# текст. «Существительные»-команды (топ, клан, ферма, биржа …) с аргументами
# намеренно не включены, чтобы не конфликтовать с названиями конкурсов.
COMMAND_ESCAPE_FIRSTWORDS = frozenset({
    "подработка", "шахта", "улучшить", "репорт", "мут", "размут", "варн",
    "разварн", "бан", "разбан", "кик", "чистка", "очистка", "закрепить",
    "открепить", "антиспам", "антимат", "антифлуд", "слоумод", "заморозка",
    "ссылки", "логи", "журнал", "выдать", "дать", "забрать", "перевод", "дуэль",
    "назначить", "повысить", "понизить", "разжаловать", "снять", "фондовый",
})


# Команды вида «<существительное> <действие> [аргумент]», где первое слово —
# обычное существительное, способное также начинать название конкурса. Поэтому
# срабатываем только при точном совпадении пары «слово + действие».
COMMAND_ESCAPE_PHRASES = frozenset({
    "кейс открыть", "магазин купить", "машина купить", "машины купить",
    "ферма собрать", "ферма купить", "ферма монета", "купить ферму", "купить рейтинг",
    "продать рейтинг", "космодром собрать", "космодром запуск", "космодром купить",
    "космодром улучшить", "биржа стакан", "биржа отмена", "биржа купить", "биржа продать",
    "клан создать", "клан вступить", "клан выйти", "топ богатых", "топ активных",
    "топ кланов", "промо список", "промо лист", "промо активировать", "фондовый рынок",
})


def _looks_like_command(text: str) -> bool:
    """Определяет, является ли сообщение командой бота (а не ответом мастера)."""
    stripped = " ".join((text or "").split())
    if not stripped:
        return False
    if stripped.startswith("/"):
        return True
    folded = stripped.casefold()
    if folded in COMMAND_ESCAPE_TEXTS:
        return True
    words = folded.split()
    if words[0] in COMMAND_ESCAPE_FIRSTWORDS:
        return True
    if len(words) >= 2 and " ".join(words[:2]) in COMMAND_ESCAPE_PHRASES:
        return True
    return False


class CommandEscapeMiddleware(BaseMiddleware):
    """Сбрасывает активное состояние FSM, когда пользователь пишет команду бота.

    Без этого пошаговый мастер (создание конкурса и т.п.) перехватывает любые
    сообщения и команды вида «работа», «баланс» или кнопки меню перестают
    отвечать, пока мастер не будет отменён вручную.
    """
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, types.Message) and data.get("raw_state"):
            if _looks_like_command(event.text or ""):
                state = data.get("state")
                if state is not None:
                    try:
                        await state.clear()
                    except Exception:
                        pass
                # Сбрасываем raw_state, чтобы хэндлеры состояний не сработали.
                data["raw_state"] = None
        return await handler(event, data)


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
                # bot обязателен: без него администратор, ещё не попавший в БД,
                # считался бы обычным игроком и получал бы авто-муты/удаления.
                admin_lvl = await get_user_admin_level(
                    session,
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    bot=message.bot,
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

                # 8. ИИ-модерация: спам-боты, реклама, скам (по умолчанию выключена).
                if settings.get("ai_moderation_enabled", False) and admin_lvl < 2:
                    verdict = None
                    bot_like_reason = looks_like_bot_name(
                        message.from_user.first_name, message.from_user.username
                    )
                    try:
                        verdict = await classify_message(
                            message.text or message.caption or "",
                            settings=settings,
                            user_name=message.from_user.first_name,
                        )
                    except Exception:
                        verdict = None
                    # Ник-спам (Crypto Signals и т.п.) сам по себе — достаточный повод.
                    if bot_like_reason and verdict is not None and not verdict.is_spam:
                        verdict = type(verdict)(
                            is_spam=True,
                            score=max(verdict.score, 0.7),
                            reason=f"спам-ник: {bot_like_reason}",
                            source="heuristic",
                        )
                    if verdict is not None and verdict.is_spam:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        action = str(settings.get("ai_moderation_action", "delete"))
                        reason = f"ИИ-модерация: {verdict.reason or 'спам'} (скор {verdict.score:.2f})"
                        try:
                            if action == "ban":
                                from app.services.moderation_service import record_ban_or_kick

                                await record_ban_or_kick(
                                    session,
                                    chat_id=message.chat.id,
                                    moderator_id=message.bot.id if message.bot else 0,
                                    target_id=message.from_user.id,
                                    action="ban",
                                    reason=reason,
                                    duration=timedelta(
                                        seconds=int(settings.get("ai_moderation_duration_seconds", 3600) or 3600)
                                    ),
                                    bot=message.bot,
                                    system=True,
                                )
                            elif action == "mute":
                                await issue_mute(
                                    session,
                                    chat_id=message.chat.id,
                                    moderator_id=message.bot.id if message.bot else 0,
                                    target_id=message.from_user.id,
                                    duration=timedelta(
                                        seconds=int(settings.get("ai_moderation_duration_seconds", 3600) or 3600)
                                    ),
                                    reason=reason,
                                    bot=message.bot,
                                    system=True,
                                )
                        except Exception:
                            pass
                        try:
                            await message.answer(
                                f"🤖 {message.from_user.first_name}, сообщение удалено ИИ-модерацией: "
                                f"{verdict.reason or 'спам'}.",
                            )
                        except Exception:
                            pass
                        return

                # 9. Активность за общение: сначала учитываем само сообщение
                #    (нужно для «сколько всего написал»), затем начисляем монеты.
                #    Монеты достаются не каждому сообщению, поэтому статистика
                #    не может опираться только на награждённые.
                body = message.text or message.caption
                if body:
                    try:
                        await record_message_activity(
                            session,
                            chat_id=message.chat.id,
                            user_id=message.from_user.id,
                            message_id=message.message_id,
                            text=body,
                        )
                    except Exception:
                        pass
                    try:
                        await reward_chat_message(
                            session,
                            chat_id=message.chat.id,
                            user_id=message.from_user.id,
                            message_id=message.message_id,
                            text=body,
                        )
                    except Exception:
                        pass

        return await handler(event, data)
