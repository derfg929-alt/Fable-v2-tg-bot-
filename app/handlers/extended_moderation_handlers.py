"""Расширенные обработчики модерации, капчи, новых участников и локальной админ-панели."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from aiogram import Router, types, F, Bot
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, IS_MEMBER, IS_NOT_MEMBER
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.session import session_scope
from sqlalchemy.dialects.postgresql import insert
from app.db.models import ChatAdmin
from app.services.player_service import get_or_create_player
from app.services.moderation_service import (
    get_user_admin_level,
    set_admin_level,
    record_ban_or_kick,
    add_link_whitelist,
    remove_link_whitelist,
    list_link_whitelist,
    parse_duration_string,
    sync_chat_creator_and_admins,
    ROLE_NAMES,
)
from app.services.chat_settings_service import (
    get_or_create_chat,
    update_chat_setting,
    apply_chat_preset,
    create_captcha,
    create_report,
    handle_report,
)
from app.services.help_service import get_greeting_text

router = Router(name="extended_moderation_router")


async def _get_target_user_id(message: types.Message, session) -> int | None:
    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        user = getattr(reply, "from_user", None)
        uid = getattr(user, "id", None)
        if isinstance(uid, int):
            return uid

    entities = getattr(message, "entities", None)
    if isinstance(entities, (list, tuple)):
        for ent in entities:
            if getattr(ent, "type", None) == "text_mention" and getattr(ent, "user", None):
                uid = getattr(ent.user, "id", None)
                if isinstance(uid, int):
                    return uid

    import re
    raw_text = getattr(message, "text", "")
    if isinstance(raw_text, str):
        match = re.search(r"@([a-zA-Z0-9_]+)", raw_text)
        if match:
            uname = match.group(1).lower()
            from app.db.models import Player
            from sqlalchemy import select, func
            p = await session.scalar(select(Player).where(func.lower(Player.username) == uname))
            if p:
                return p.telegram_id

        parts = raw_text.split()
        for part in parts[1:]:
            if part.isdigit() and len(part) >= 6:
                return int(part)
    return None


# ===================== ВХОД НОВОГО УЧАСТНИКА / ПРИВЕТСТВИЕ И КАПЧА =====================

@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_bot_added_my_chat_member(event: types.ChatMemberUpdated, bot: Bot):
    """Срабатывает при добавлении самого бота в группу/канал."""
    chat = event.chat
    if event.new_chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        adder = getattr(event, "from_user", None)
        async with session_scope() as session:
            await get_or_create_chat(session, chat_id=chat.id, title=chat.title or "")
            if adder and not getattr(adder, "is_bot", False):
                await get_or_create_player(session, telegram_id=adder.id, first_name=getattr(adder, "first_name", "Пользователь"), username=getattr(adder, "username", None))

            # Синхронизируем только истинного создателя группы (он получает ур. 5)
            await sync_chat_creator_and_admins(session, chat_id=chat.id, bot=bot)

        await bot.send_message(chat_id=chat.id, text=get_greeting_text(), parse_mode="HTML")


@router.message(F.new_chat_members)
async def on_bot_added_new_chat_members(message: types.Message, bot: Bot):
    """Срабатывает при добавлении новых участников, если среди них есть наш бот."""
    if not message.new_chat_members:
        return
    bot_info = await bot.get_me()
    for member in message.new_chat_members:
        if member.id == bot_info.id:
            adder = getattr(message, "from_user", None)
            async with session_scope() as session:
                await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
                if adder and not getattr(adder, "is_bot", False):
                    await get_or_create_player(session, telegram_id=adder.id, first_name=getattr(adder, "first_name", "Пользователь"), username=getattr(adder, "username", None))

                # Синхронизируем только истинного владельца группы (creator)
                await sync_chat_creator_and_admins(session, chat_id=message.chat.id, bot=bot)

            await message.answer(get_greeting_text(), parse_mode="HTML")
            break


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_chat_member(event: types.ChatMemberUpdated, bot: Bot):
    chat = event.chat
    new_user = event.new_chat_member.user
    if new_user.is_bot:
        return

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=new_user.id, first_name=new_user.first_name, username=new_user.username)
        chat_db = await get_or_create_chat(session, chat_id=chat.id, title=chat.title or "")
        settings = chat_db.settings or {}

        # 1. Проверка капчи
        if settings.get("captcha_enabled", False):
            ch = await create_captcha(session, chat_id=chat.id, user_id=new_user.id)
            builder = InlineKeyboardBuilder()
            builder.button(text="🤖 Я человек (пройти капчу)", callback_data=f"captcha_solve_{ch.challenge_token}")
            await bot.send_message(
                chat_id=chat.id,
                text=f"👋 Привет, {new_user.first_name}! Пройди капчу в течение 2 минут, нажав кнопку ниже.",
                reply_markup=builder.as_markup(),
            )
            return

        # 2. Приветствие
        if settings.get("welcome_enabled", True):
            tpl = settings.get("welcome_text", "Добро пожаловать, {name}!\nЯ Хранитель, настроить — настройки, правила — правила, команды — команды.")
            welcome_msg = tpl.replace("{name}", new_user.first_name)
            await bot.send_message(chat_id=chat.id, text=welcome_msg)


# ===================== КИК / БАН / РАЗБАН =====================

@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in [
    "кик", "!кик", "/kick", "кикнуть", "!кикнуть"
])))
async def handle_kick(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>кик [причина]</code> или <code>кик @user [причина]</code>", parse_mode="HTML")
            return

        parts = (message.text or "").split()
        reason = " ".join([p for p in parts[1:] if not p.startswith("@") and p != str(target_id)]) or "Исключение модератором"
        try:
            await record_ban_or_kick(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, action="kick", reason=reason, bot=bot)
            tg_status = ""
            try:
                await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
                await bot.unban_chat_member(chat_id=message.chat.id, user_id=target_id)
                tg_status = " <i>(участник исключен из группы Telegram)</i>"
            except Exception as tg_err:
                tg_status = f"\n⚠️ <i>Примечание: Telegram не дал исключить участника ({tg_err}). Проверьте права бота (требуется админ с правом бана)!</i>"
            await message.reply(
                f"👢 <b>ПОЛЬЗОВАТЕЛЬ ИСКЛЮЧЕН (КИК)!</b>\n\n"
                f"👤 Нарушитель: ID <code>{target_id}</code>\n"
                f"📝 Причина: <i>{reason}</i>\n"
                f"👮‍♂️ Модератор: ID <code>{message.from_user.id}</code>{tg_status}",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in [
    "бан", "!бан", "/ban", "забанить", "!забанить"
])))
async def handle_ban(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>бан [причина]</code> или <code>бан @user [причина]</code>", parse_mode="HTML")
            return

        parts = (message.text or "").split()
        reason = " ".join([p for p in parts[1:] if not p.startswith("@") and p != str(target_id)]) or "Блокировка модератором"
        try:
            await record_ban_or_kick(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, action="ban", reason=reason, bot=bot)
            tg_status = ""
            try:
                await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
                tg_status = " <i>(в Telegram заблокирован)</i>"
            except Exception as tg_err:
                tg_status = f"\n⚠️ <i>Примечание: Telegram не дал заблокировать участника ({tg_err}). Проверьте права бота (требуется админ с правом бана)!</i>"
            await message.reply(
                f"⛔️ <b>ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН (БАН)!</b>\n\n"
                f"👤 Нарушитель: ID <code>{target_id}</code>\n"
                f"📝 Причина: <i>{reason}</i>\n"
                f"👮‍♂️ Администратор: ID <code>{message.from_user.id}</code>{tg_status}",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in [
    "разбан", "!разбан", "/unban", "снять бан"
])))
async def handle_unban(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>разбан</code> или <code>разбан @user</code>", parse_mode="HTML")
            return
        try:
            lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
            if lvl < 3:
                await message.reply("Для разбана требуется уровень 3 (Админ)")
                return
            tg_status = ""
            try:
                await bot.unban_chat_member(chat_id=message.chat.id, user_id=target_id, only_if_banned=True)
                tg_status = " <i>(в Telegram бан снят)</i>"
            except Exception as tg_err:
                tg_status = f"\n⚠️ <i>Примечание: Telegram unban ({tg_err}).</i>"
            await message.reply(
                f"✅ <b>ПОЛЬЗОВАТЕЛЬ РАЗБЛОКИРОВАН!</b>\n\n"
                f"👤 ID <code>{target_id}</code> снова может вступать в группу.{tg_status}",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


# ===================== ЗАМОРОЗКА И СЛОУМОД =====================

@router.message(F.text.casefold().startswith("заморозка"))
async def handle_freeze(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    text = (message.text or "").strip().casefold()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            await message.reply("Управлять заморозкой может Админ (ур. 3+)")
            return

        if "выкл" in text:
            await update_chat_setting(session, chat_id=message.chat.id, admin_id=message.from_user.id, key="frozen_until", value=None, min_level=3)
            await message.reply("🔓 Заморозка чата снята. Участники снова могут писать сообщения.")
            return

        parts = text.split()
        dur_str = parts[2] if len(parts) >= 3 else "10м"
        try:
            dur = parse_duration_string(dur_str)
            until = datetime.now(timezone.utc) + dur
            await update_chat_setting(session, chat_id=message.chat.id, admin_id=message.from_user.id, key="frozen_until", value=until.isoformat(), min_level=3)
            await message.reply(f"❄️ Чат заморожен на {dur_str}. Обычные участники временно не могут отправлять сообщения.")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("слоумод"))
async def handle_slowmode(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    text = (message.text or "").strip().casefold()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            await message.reply("Управлять слоумодом может Админ (ур. 3+)")
            return

        if "выкл" in text:
            try:
                await bot.set_chat_slow_mode_delay(chat_id=message.chat.id, slow_mode_delay=0)
            except Exception:
                pass
            await update_chat_setting(session, chat_id=message.chat.id, admin_id=message.from_user.id, key="slowmode_seconds", value=0, min_level=3)
            await message.reply("🚀 Слоумод отключен.")
            return

        parts = text.split()
        if len(parts) >= 2 and parts[1].isdigit():
            sec = int(parts[1])
            try:
                await bot.set_chat_slow_mode_delay(chat_id=message.chat.id, slow_mode_delay=sec)
            except Exception:
                pass
            await update_chat_setting(session, chat_id=message.chat.id, admin_id=message.from_user.id, key="slowmode_seconds", value=sec, min_level=3)
            await message.reply(f"🐢 Слоумод установлен на {sec} сек.")


# ===================== РЕПОРТЫ =====================

@router.message(F.text.casefold().startswith("репорт"))
async def handle_report_cmd(message: types.Message):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    reason = " ".join((message.text or "").split()[1:]) or "Жалоба на нарушение"

    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        rep = await create_report(
            session,
            chat_id=message.chat.id,
            reporter_id=message.from_user.id,
            target_id=target_id,
            reason=reason,
            message_id=message.reply_to_message.message_id if message.reply_to_message else None,
            message_text=message.reply_to_message.text if message.reply_to_message else None,
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Принять", callback_data=f"rep_acc_{rep.id}")
        builder.button(text="❌ Отклонить", callback_data=f"rep_rej_{rep.id}")
        builder.adjust(2)

        await message.reply(
            f"🚩 <b>Создан тикет жалобы #{rep.id}</b>\n"
            f"Причина: {reason}\n"
            f"Модераторы уведомлены.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("rep_acc_"))
async def on_report_accept(callback: types.CallbackQuery):
    rep_id = int(callback.data.split("_")[2])
    async with session_scope() as session:
        try:
            await handle_report(session, report_id=rep_id, moderator_id=callback.from_user.id, resolution="accepted")
            try:
                await callback.answer("Жалоба принята!")
            except Exception:
                pass
            try:
                await callback.message.edit_text(f"🚩 Жалоба #{rep_id} ПРИНЯТА модератором ID {callback.from_user.id}.")
            except Exception:
                pass
        except Exception as e:
            try:
                await callback.answer(f"❌ {e}", show_alert=True)
            except Exception:
                pass


@router.callback_query(F.data.startswith("rep_rej_"))
async def on_report_reject(callback: types.CallbackQuery):
    rep_id = int(callback.data.split("_")[2])
    async with session_scope() as session:
        try:
            await handle_report(session, report_id=rep_id, moderator_id=callback.from_user.id, resolution="rejected")
            try:
                await callback.answer("Жалоба отклонена!")
            except Exception:
                pass
            try:
                await callback.message.edit_text(f"🚩 Жалоба #{rep_id} ОТКЛОНЕНА модератором ID {callback.from_user.id}.")
            except Exception:
                pass
        except Exception as e:
            try:
                await callback.answer(f"❌ {e}", show_alert=True)
            except Exception:
                pass


# ===================== НАЗНАЧЕНИЕ АДМИНОВ / ССЫЛКИ / НАСТРОЙКИ ЧАТА =====================

@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in [
    "админ назначить", "!админ назначить", "дать админку", "!дать админку", "назначить админа",
    "повысить", "!повысить", "+админ", "!+админ"
])))
async def handle_admin_appoint(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    parts = (message.text or "").split()
    lvl_val = None
    for p in reversed(parts):
        if p.isdigit() and int(p) <= 5:
            lvl_val = int(p)
            break

    # Если уровень не указан явно, по умолчанию назначаем уровень 3 (Админ)
    if lvl_val is None:
        lvl_val = 3

    async with session_scope() as session:
        await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply(
                "💡 <b>Использование:</b> ответом на сообщение <code>админ назначить 3</code>\n"
                "или <code>админ назначить @user [уровень 2..5]</code> (по умолчанию ур. 3)",
                parse_mode="HTML",
            )
            return

        try:
            new_lvl = await set_admin_level(session, chat_id=message.chat.id, actor_id=message.from_user.id, target_id=target_id, new_level=lvl_val, bot=bot)
            await message.reply(f"👑 Пользователю ID <code>{target_id}</code> успешно установлен уровень {new_lvl} ({ROLE_NAMES[new_lvl]})!", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in [
    "админ снять", "!админ снять", "снять админку", "!снять админку", "снять админа",
    "понизить", "!понизить", "-админ", "!-админ", "разжаловать"
])))
async def handle_admin_dismiss(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    async with session_scope() as session:
        await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply(
                "💡 <b>Использование:</b> ответом на сообщение <code>админ снять</code> или <code>админ снять @user</code>",
                parse_mode="HTML",
            )
            return

        try:
            await set_admin_level(session, chat_id=message.chat.id, actor_id=message.from_user.id, target_id=target_id, new_level=1, bot=bot)
            await message.reply(f"🛡 С пользователя ID <code>{target_id}</code> успешно сняты права администратора в чате.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("ссылки"))
async def handle_whitelist(message: types.Message):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    parts = (message.text or "").split()
    if len(parts) == 1 or parts[1].casefold() == "список":
        async with session_scope() as session:
            domains = await list_link_whitelist(session, chat_id=message.chat.id)
            if not domains:
                await message.reply("Белый список доменов пуст (все ссылки блокируются).")
                return
            lines = ["🌐 <b>Белый список ссылок чата:</b>\n"]
            for d in domains:
                lines.append(f"• <code>{d}</code>")
            await message.reply("\n".join(lines), parse_mode="HTML")
            return

    cmd = parts[1].casefold()
    if cmd in ("добавить", "add") and len(parts) >= 3:
        domain = parts[2]
        async with session_scope() as session:
            try:
                await add_link_whitelist(session, chat_id=message.chat.id, admin_id=message.from_user.id, domain=domain)
                await message.reply(f"✅ Домен <code>{domain}</code> добавлен в белый список.", parse_mode="HTML")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

    if cmd in ("удалить", "del") and len(parts) >= 3:
        domain = parts[2]
        async with session_scope() as session:
            try:
                ok = await remove_link_whitelist(session, chat_id=message.chat.id, admin_id=message.from_user.id, domain=domain)
                await message.reply("✅ Домен удален из белого списка." if ok else "Домен не найден в белом списке.")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return


@router.message(F.text.casefold().in_([
    "настройки", "!настройки", "/settings", "админка", "!админка", "/admin", "админ панель", "!админ панель"
]))
async def handle_chat_settings_panel(message: types.Message, bot: Bot):
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    async with session_scope() as session:
        # Автоматическая синхронизация истинного создателя чата (ур. 5) и администраторов из Telegram
        await sync_chat_creator_and_admins(session, chat_id=message.chat.id, bot=bot)
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            await message.reply("Панель настроек и админка доступны администраторам чата уровня 3+ (Админ, Старший админ, Создатель)")
            return

        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        s = chat.settings or {}

        builder = InlineKeyboardBuilder()
        builder.button(text="🛡 Строгий пресет", callback_data=f"preset_strict_{message.chat.id}")
        builder.button(text="⚖️ Обычный пресет", callback_data=f"preset_normal_{message.chat.id}")
        builder.button(text="🏖 Казуальный", callback_data=f"preset_casual_{message.chat.id}")
        builder.adjust(3)

        text = (
            f"⚙️ <b>Панель настроек чата «{message.chat.title}»</b>\n\n"
            f"👑 Ваш статус: <b>{ROLE_NAMES.get(lvl, 'Админ')} (ур. {lvl})</b>\n\n"
            f"Текущий пресет: <b>{s.get('preset', 'normal')}</b>\n"
            f"Антиссылки: <b>{'Вкл' if s.get('links_restricted', True) else 'Выкл'}</b>\n"
            f"Капча при входе: <b>{'Вкл' if s.get('captcha_enabled', False) else 'Выкл'}</b>\n"
            f"Обязательные правила: <b>{'Вкл' if s.get('rules_acceptance_required', False) else 'Выкл'}</b>\n"
            f"Приветствие: <b>{'Вкл' if s.get('welcome_enabled', True) else 'Выкл'}</b>\n\n"
            f"<i>Выберите пресет для быстрой настройки:</i>"
        )
        await message.reply(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("preset_"))
async def on_preset_select(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    preset_name = parts[1]
    chat_id = int(parts[2])
    async with session_scope() as session:
        try:
            await apply_chat_preset(session, chat_id=chat_id, admin_id=callback.from_user.id, preset_name=preset_name)
            try:
                await callback.answer(f"Пресет {preset_name.upper()} применен!")
            except Exception:
                pass
            try:
                await callback.message.edit_text(f"✅ Для чата успешно применен пресет: <b>{preset_name.upper()}</b>!", parse_mode="HTML")
            except Exception:
                pass
        except Exception as e:
            try:
                await callback.answer(f"❌ {e}", show_alert=True)
            except Exception:
                pass
