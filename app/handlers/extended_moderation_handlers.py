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
    extract_moderation_args,
    sync_chat_creator_and_admins,
    _format_duration_ru,
    ROLE_NAMES,
)
from app.services.chat_settings_service import (
    get_or_create_chat,
    update_chat_setting,
    apply_chat_preset,
    create_captcha,
    create_report,
    handle_report,
    DEFAULT_CHAT_SETTINGS,
)
from app.services.antispam_service import DEFAULT_ANTISPAM, antispam_status_line
from app.services.logging_service import notify_moderation_log
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

        from app.services.moderation_service import ensure_player_and_chat_exist
        await ensure_player_and_chat_exist(
            session,
            chat_id=message.chat.id,
            user_ids=[message.from_user.id, target_id],
            bot=bot,
        )

        parts = (message.text or "").split()
        reason = " ".join([p for p in parts[1:] if not p.startswith("@") and p != str(target_id)]) or "Исключение модератором"
        try:
            await record_ban_or_kick(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, action="kick", reason=reason, bot=bot)
            await notify_moderation_log(
                session,
                bot=bot,
                chat_id=message.chat.id,
                text=(
                    f"👢 <b>КИК</b>\nЦель: <code>{target_id}</code>\n"
                    f"Причина: {reason}\nМодератор: <code>{message.from_user.id}</code>"
                ),
            )
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
    raw_text = (message.text or "").strip()
    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>бан [срок] [причина]</code> или <code>бан @user [срок] [причина]</code>", parse_mode="HTML")
            return

        from app.services.moderation_service import ensure_player_and_chat_exist
        await ensure_player_and_chat_exist(
            session,
            chat_id=message.chat.id,
            user_ids=[message.from_user.id, target_id],
            bot=bot,
        )

        dur, dur_str, reason = extract_moderation_args(
            full_text=raw_text,
            target_id=target_id,
            default_duration=None,
        )
        try:
            case = await record_ban_or_kick(
                session,
                chat_id=message.chat.id,
                moderator_id=message.from_user.id,
                target_id=target_id,
                action="ban",
                reason=reason,
                duration=dur,
                bot=bot,
            )
            await notify_moderation_log(
                session,
                bot=bot,
                chat_id=message.chat.id,
                text=(
                    f"⛔️ <b>БАН</b>\nЦель: <code>{target_id}</code>\n"
                    f"Срок: <b>{dur_str or 'навсегда'}</b>\nПричина: {reason}\n"
                    f"Администратор: <code>{message.from_user.id}</code>"
                ),
            )
            tg_status = ""
            try:
                until_date = case.expires_at if case.expires_at else None
                await bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id, until_date=until_date)
                tg_status = " <i>(в Telegram заблокирован)</i>"
            except Exception as tg_err:
                tg_status = f"\n⚠️ <i>Примечание: Telegram не дал заблокировать участника ({tg_err}). Проверьте права бота (требуется админ с правом бана)!</i>"

            time_line = f"⏱ Срок бана: <b>{dur_str}</b>\n" if dur_str else ""
            await message.reply(
                f"⛔️ <b>ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН (БАН)!</b>\n\n"
                f"👤 Нарушитель: ID <code>{target_id}</code>\n"
                f"{time_line}"
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
        from app.services.moderation_service import ensure_player_and_chat_exist
        await ensure_player_and_chat_exist(
            session,
            chat_id=message.chat.id,
            user_ids=[message.from_user.id, target_id],
            bot=bot,
        )
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
            from app.services.moderation_service import revoke_ban
            try:
                await revoke_ban(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, bot=bot)
            except Exception:
                pass
            await notify_moderation_log(
                session,
                bot=bot,
                chat_id=message.chat.id,
                text=(
                    f"✅ <b>РАЗБАН</b>\nЦель: <code>{target_id}</code>\n"
                    f"Администратор: <code>{message.from_user.id}</code>"
                ),
            )
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

        rest_freeze = text.split(maxsplit=1)[1] if len(text.split()) > 1 else "10м"
        try:
            dur = parse_duration_string(rest_freeze)
            until = datetime.now(timezone.utc) + dur
            await update_chat_setting(session, chat_id=message.chat.id, admin_id=message.from_user.id, key="frozen_until", value=until.isoformat(), min_level=3)
            await message.reply(f"❄️ Чат заморожен на {rest_freeze}. Обычные участники временно не могут отправлять сообщения.")
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
        builder.button(
            text=f"🔗 Антиссылки: {'Вкл' if s.get('links_restricted') else 'Выкл'}",
            callback_data=f"cfg_toggle_links_{message.chat.id}",
        )
        builder.button(
            text=f"🤖 Капча: {'Вкл' if s.get('captcha_enabled') else 'Выкл'}",
            callback_data=f"cfg_toggle_captcha_{message.chat.id}",
        )
        builder.button(
            text=f"🛡 Антиспам: {'Вкл' if s.get('antispam_enabled') else 'Выкл'}",
            callback_data=f"cfg_toggle_antispam_{message.chat.id}",
        )
        builder.button(
            text=f"🤬 Антимат: {'Вкл' if s.get('badwords_enabled') else 'Выкл'}",
            callback_data=f"cfg_toggle_badwords_{message.chat.id}",
        )
        builder.button(
            text=f"📜 Правила: {'Вкл' if s.get('rules_acceptance_required') else 'Выкл'}",
            callback_data=f"cfg_toggle_rules_{message.chat.id}",
        )
        builder.button(
            text=f"👋 Приветствие: {'Вкл' if s.get('welcome_enabled', True) else 'Выкл'}",
            callback_data=f"cfg_toggle_welcome_{message.chat.id}",
        )
        builder.adjust(3, 2, 2, 1)

        log_channel = chat.log_chat_id or s.get("log_channel_id")
        warn_limit = int(s.get("warn_limit", 5) or 0)
        warn_action = s.get("warn_action", "mute")
        text = (
            f"⚙️ <b>Панель настроек чата «{message.chat.title}»</b>\n\n"
            f"👑 Ваш статус: <b>{ROLE_NAMES.get(lvl, 'Админ')} (ур. {lvl})</b>\n\n"
            f"Текущий пресет: <b>{s.get('preset', 'normal')}</b>\n"
            f"Антиссылки: <b>{'Вкл' if s.get('links_restricted', False) else 'Выкл'}</b>\n"
            f"Капча при входе: <b>{'Вкл' if s.get('captcha_enabled', False) else 'Выкл'}</b>\n"
            f"Антиспам: <b>{'Вкл' if s.get('antispam_enabled', False) else 'Выкл'}</b>\n"
            f"Антимат: <b>{'Вкл' if s.get('badwords_enabled', False) else 'Выкл'}</b>\n"
            f"Обязательные правила: <b>{'Вкл' if s.get('rules_acceptance_required', False) else 'Выкл'}</b>\n"
            f"Приветствие: <b>{'Вкл' if s.get('welcome_enabled', True) else 'Выкл'}</b>\n"
            f"Варны: лимит <b>{warn_limit or '∞'}</b>, действие <b>{warn_action}</b>\n"
            f"Лог-канал: <b>{log_channel if log_channel else 'не настроен'}</b>\n\n"
            f"<b>Команды настройки:</b>\n"
            f"• <code>антиспам вкл|выкл</code>, <code>антиспам лимит 6 10</code>, <code>антиспам действие бан</code>\n"
            f"• <code>антимат вкл|выкл</code>, <code>антимат действие удалить|варн|мут</code>\n"
            f"• <code>варн настройки</code> — лимит, действие и срок авто-наказания\n"
            f"• <code>+капча</code> / <code>-капча</code> — капча при входе\n"
            f"• <code>логи настроить ID</code> — логи модерации\n"
            f"• <code>ссылки добавить домен</code> — белый список ссылок\n\n"
            f"<i>Нажмите кнопку ниже, чтобы быстро переключить настройку, или выберите пресет:</i>"
        )
        await message.reply(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("cfg_toggle_"))
async def on_setting_toggle(callback: types.CallbackQuery):
    """Быстрое переключение настройки чата из панели (ур. 3+)."""
    try:
        _, _, key, chat_id_str = callback.data.split("_", 3)
        chat_id = int(chat_id_str)
    except Exception:
        try:
            await callback.answer("❌ Некорректная кнопка", show_alert=True)
        except Exception:
            pass
        return

    mapping = {
        "links": "links_restricted",
        "captcha": "captcha_enabled",
        "antispam": "antispam_enabled",
        "badwords": "badwords_enabled",
        "rules": "rules_acceptance_required",
        "welcome": "welcome_enabled",
    }
    setting_key = mapping.get(key)
    if not setting_key:
        try:
            await callback.answer("❌ Неизвестная настройка", show_alert=True)
        except Exception:
            pass
        return

    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=callback.from_user.id, bot=callback.bot)
        if lvl < 3:
            try:
                await callback.answer("❌ Требуется уровень 3+", show_alert=True)
            except Exception:
                pass
            return
        chat = await get_or_create_chat(session, chat_id=chat_id, title=callback.message.chat.title if callback.message else "")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        current = settings.get(setting_key, True if setting_key == "welcome_enabled" else False)
        settings[setting_key] = not bool(current)
        chat.settings = settings
        await session.flush()
        new_state = "Вкл" if settings[setting_key] else "Выкл"

    try:
        await callback.answer(f"✅ {setting_key}: {new_state}")
    except Exception:
        pass
    # Обновляем текст панели, перерисовывая её заново.
    try:
        await _refresh_settings_panel(callback, chat_id)
    except Exception:
        pass


async def _refresh_settings_panel(callback: types.CallbackQuery, chat_id: int) -> None:
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id)
        s = chat.settings or {}
        lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=callback.from_user.id, bot=callback.bot)

        builder = InlineKeyboardBuilder()
        builder.button(text="🛡 Строгий пресет", callback_data=f"preset_strict_{chat_id}")
        builder.button(text="⚖️ Обычный пресет", callback_data=f"preset_normal_{chat_id}")
        builder.button(text="🏖 Казуальный", callback_data=f"preset_casual_{chat_id}")
        builder.adjust(3)
        builder.button(text=f"🔗 Антиссылки: {'Вкл' if s.get('links_restricted') else 'Выкл'}", callback_data=f"cfg_toggle_links_{chat_id}")
        builder.button(text=f"🤖 Капча: {'Вкл' if s.get('captcha_enabled') else 'Выкл'}", callback_data=f"cfg_toggle_captcha_{chat_id}")
        builder.button(text=f"🛡 Антиспам: {'Вкл' if s.get('antispam_enabled') else 'Выкл'}", callback_data=f"cfg_toggle_antispam_{chat_id}")
        builder.button(text=f"🤬 Антимат: {'Вкл' if s.get('badwords_enabled') else 'Выкл'}", callback_data=f"cfg_toggle_badwords_{chat_id}")
        builder.button(text=f"📜 Правила: {'Вкл' if s.get('rules_acceptance_required') else 'Выкл'}", callback_data=f"cfg_toggle_rules_{chat_id}")
        builder.button(text=f"👋 Приветствие: {'Вкл' if s.get('welcome_enabled', True) else 'Выкл'}", callback_data=f"cfg_toggle_welcome_{chat_id}")
        builder.adjust(3, 2, 2, 1)

        warn_limit = int(s.get("warn_limit", 5) or 0)
        warn_action = s.get("warn_action", "mute")
        text = (
            f"⚙️ <b>Панель настроек чата</b>\n\n"
            f"👑 Ваш статус: <b>{ROLE_NAMES.get(lvl, 'Админ')} (ур. {lvl})</b>\n\n"
            f"Текущий пресет: <b>{s.get('preset', 'normal')}</b>\n"
            f"Антиссылки: <b>{'Вкл' if s.get('links_restricted') else 'Выкл'}</b>\n"
            f"Капча: <b>{'Вкл' if s.get('captcha_enabled') else 'Выкл'}</b>\n"
            f"Антиспам: <b>{'Вкл' if s.get('antispam_enabled') else 'Выкл'}</b>\n"
            f"Антимат: <b>{'Вкл' if s.get('badwords_enabled') else 'Выкл'}</b>\n"
            f"Обязательные правила: <b>{'Вкл' if s.get('rules_acceptance_required') else 'Выкл'}</b>\n"
            f"Приветствие: <b>{'Вкл' if s.get('welcome_enabled', True) else 'Выкл'}</b>\n"
            f"Варны: лимит <b>{warn_limit or '∞'}</b>, действие <b>{warn_action}</b>\n\n"
            f"<i>Нажмите кнопку, чтобы переключить настройку.</i>"
        )
    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception:
        pass


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


# ===================== ЛОГИ МОДЕРАЦИИ (ЛОГ-КАНАЛ) =====================

async def _send_log_channel_help(message: types.Message, bot: Bot, chat_id: int, admin_id: int) -> None:
    await message.reply(
        "🛡 <b>Логи модерации</b>\n\n"
        "Настройте канал, куда будут приходить все действия модерации "
        "(муты, баны, варны, кики, авто-антиспам, авто-снятие наказаний).\n\n"
        "<b>Как настроить:</b>\n"
        "1. Добавьте бота администратором в канал/группу для логов.\n"
        "2. Отправьте в этом чате команду <code>логи настроить ID_канала</code> "
        "(например: <code>логи настроить -1001234567890</code>).\n"
        "3. Отключить: <code>логи выкл</code>.\n\n"
        "<i>Требуется уровень 3+ (Админ).</i>",
        parse_mode="HTML",
    )


@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "логи", "!логи", "/logs", "логи модерации", "лог", "!лог", "журнал", "!журнал"
])))
async def handle_moderation_logs(message: types.Message, bot: Bot):
    """Управление лог-каналом модерации: просмотр, настройка, отключение."""
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    parts = (message.text or "").split()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            # Молча игнорируем: обычные слова не должны вызывать ответ бота.
            return

        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        current = chat.log_chat_id or (chat.settings or {}).get("log_channel_id")

        if len(parts) == 1 or parts[1].casefold() in ("статус", "status", "список"):
            if current:
                await message.reply(f"🛡 Лог-канал модерации: <code>{current}</code>", parse_mode="HTML")
            else:
                await _send_log_channel_help(message, bot, message.chat.id, message.from_user.id)
            return

        cmd = parts[1].casefold()
        if cmd in ("настроить", "установить", "set", "вкл", "включить") and len(parts) >= 3:
            raw = parts[2].strip()
            try:
                target = int(raw)
            except ValueError:
                await message.reply("❌ Укажите ID канала числом, например: <code>логи настроить -1001234567890</code>", parse_mode="HTML")
                return
            chat.log_chat_id = target
            settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
            settings["log_channel_id"] = target
            chat.settings = settings
            await session.flush()
            await message.reply(
                f"✅ Логи модерации будут отправляться в канал <code>{target}</code>.\n"
                f"<i>Убедитесь, что бот добавлен туда администратором.</i>",
                parse_mode="HTML",
            )
            return

        if cmd in ("выкл", "отключить", "off", "убрать"):
            chat.log_chat_id = None
            settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
            settings["log_channel_id"] = None
            chat.settings = settings
            await session.flush()
            await message.reply("🛑 Логи модерации отключены.")
            return

        await _send_log_channel_help(message, bot, message.chat.id, message.from_user.id)


# ===================== АВТОМАТИЧЕСКИЙ АНТИСПАМ =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "антиспам", "!антиспам", "/antispam"
])))
async def handle_antispam_settings(message: types.Message, bot: Bot):
    """Включение/выключение и настройка автоматического антиспама."""
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    parts = (message.text or "").split()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            return

        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)

        def _persist() -> None:
            chat.settings = settings

        if len(parts) == 1:
            dur = int(settings.get("antispam_duration_seconds", DEFAULT_ANTISPAM["antispam_duration_seconds"])) // 60
            await message.reply(
                f"🛡 <b>Автоматический антиспам</b>\n\n"
                f"Статус: <b>{'Вкл' if settings.get('antispam_enabled') else 'Выкл'}</b>\n"
                f"Лимит: <b>{settings.get('antispam_max_messages', DEFAULT_ANTISPAM['antispam_max_messages'])}</b> "
                f"сообщений за <b>{settings.get('antispam_window_seconds', DEFAULT_ANTISPAM['antispam_window_seconds'])}</b> сек.\n"
                f"Действие: <b>{settings.get('antispam_action', DEFAULT_ANTISPAM['antispam_action'])}</b>, "
                f"срок <b>{dur} мин.</b>\n"
                f"Порог повторов: <b>{settings.get('antispam_duplicate_threshold', DEFAULT_ANTISPAM['antispam_duplicate_threshold'])}</b>\n\n"
                f"<b>Управление:</b>\n"
                f"• <code>антиспам вкл</code> / <code>антиспам выкл</code>\n"
                f"• <code>антиспам срок 30м</code> — длительность наказания\n"
                f"• <code>антиспам лимит 6 10</code> — N сообщений за W секунд\n"
                f"• <code>антиспам действие бан</code> / <code>антиспам действие мут</code>",
                parse_mode="HTML",
            )
            return

        cmd = parts[1].casefold()
        if cmd in ("вкл", "включить", "on"):
            settings["antispam_enabled"] = True
            _persist()
            await session.flush()
            await message.reply("🛡 Автоматический антиспам <b>включён</b>. Флудеры получат авто-мут.", parse_mode="HTML")
            return
        if cmd in ("выкл", "отключить", "off"):
            settings["antispam_enabled"] = False
            _persist()
            await session.flush()
            await message.reply("🛑 Автоматический антиспам <b>выключен</b>.")
            return
        if cmd in ("срок", "время") and len(parts) >= 3:
            try:
                dur = parse_duration_string(parts[2])
            except Exception as e:
                await message.reply(f"❌ {e}")
                return
            settings["antispam_duration_seconds"] = int(dur.total_seconds())
            _persist()
            await session.flush()
            await message.reply(f"⏱ Срок авто-наказания: <b>{int(dur.total_seconds()) // 60} мин.</b>", parse_mode="HTML")
            return
        if cmd in ("лимит", "limit") and len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
            settings["antispam_max_messages"] = max(2, int(parts[2]))
            settings["antispam_window_seconds"] = max(1, int(parts[3]))
            _persist()
            await session.flush()
            await message.reply(
                f"📊 Лимит: <b>{settings['antispam_max_messages']}</b> сообщений за "
                f"<b>{settings['antispam_window_seconds']}</b> сек.",
                parse_mode="HTML",
            )
            return
        if cmd in ("действие", "action") and len(parts) >= 3:
            act = parts[2].casefold()
            if act in ("бан", "ban"):
                settings["antispam_action"] = "ban"
            elif act in ("мут", "mute"):
                settings["antispam_action"] = "mute"
            else:
                await message.reply("Укажите: <code>антиспам действие бан</code> или <code>антиспам действие мут</code>", parse_mode="HTML")
                return
            _persist()
            await session.flush()
            await message.reply(f"⚙️ Авто-наказание: <b>{settings['antispam_action']}</b>")
            return
        if cmd in ("повторы", "дубликаты", "dupes") and len(parts) >= 3 and parts[2].isdigit():
            settings["antispam_duplicate_threshold"] = max(2, int(parts[2]))
            _persist()
            await session.flush()
            await message.reply(f"🔁 Порог повторов: <b>{settings['antispam_duplicate_threshold']}</b> подряд одинаковых сообщений.")
            return
        if cmd in ("эскалация", "escalate") and len(parts) >= 3 and parts[2].isdigit():
            settings["antispam_escalate_after"] = max(1, int(parts[2]))
            _persist()
            await session.flush()
            await message.reply(f"⬆️ Эскалация до бана после <b>{settings['antispam_escalate_after']}</b> срабатываний.")
            return
        if cmd in ("капс", "caps") and len(parts) >= 3:
            try:
                ratio = float(parts[2].replace(",", "."))
            except ValueError:
                await message.reply("Укажите долю заглавных букв, например: <code>антиспам капс 0.7</code> (0 — выключить)")
                return
            settings["antispam_caps_ratio"] = max(0.0, min(1.0, ratio))
            _persist()
            await session.flush()
            await message.reply(f"🔠 Порог капса: <b>{settings['antispam_caps_ratio']}</b> (0 — выключено).")
            return
        if cmd in ("эмодзи", "emoji") and len(parts) >= 3 and parts[2].isdigit():
            settings["antispam_emoji_limit"] = int(parts[2])
            _persist()
            await session.flush()
            await message.reply(f"😀 Лимит эмодзи в сообщении: <b>{settings['antispam_emoji_limit']}</b> (0 — выключено).")
            return

        await message.reply("Неизвестная настройка антиспама. Напишите <code>антиспам</code> для справки.", parse_mode="HTML")


# ===================== СПРАВКА ПО МОДЕРАЦИИ =====================

@router.message(F.text.casefold().in_([
    "модерация", "!модерация", "команды модерации", "!команды модерации",
    "/modhelp", "модхелп", "помощь модерация",
]))
async def handle_moderation_help(message: types.Message):
    """Показывает справку по всем командам модерации и авто-модерации."""
    from app.services.help_service import MODERATION_HELP_TEXT

    await message.reply(MODERATION_HELP_TEXT, parse_mode="HTML")


# ===================== АНТИ-ФЛУД «ПАУЗА» =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "антифлуд пауза", "флуд пауза", "!антифлуд пауза", "антифлуд"
])))
async def handle_antiflood_pause(message: types.Message, bot: Bot):
    """Минимальный интервал между сообщениями одного игрока (анти-флуд «пауза»)."""
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    parts = (message.text or "").split()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            return
        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)

        if len(parts) >= 3 and parts[2].casefold() in ("выкл", "off", "0"):
            settings["antiflood_pause_seconds"] = 0
            chat.settings = settings
            await session.flush()
            await message.reply("🛑 Анти-флуд «пауза» выключен.")
            return
        if len(parts) >= 3:
            try:
                dur = parse_duration_string(" ".join(parts[2:]))
                seconds = int(dur.total_seconds())
            except Exception:
                seconds = int(parts[2]) if parts[2].isdigit() else 0
            settings["antiflood_pause_seconds"] = max(0, seconds)
            chat.settings = settings
            await session.flush()
            await message.reply(
                f"⏳ Анти-флуд «пауза»: минимальный интервал <b>{_format_duration_ru(timedelta(seconds=seconds)) if seconds else 'выкл'}</b> "
                f"между сообщениями игрока.",
                parse_mode="HTML",
            )
            return

        cur = int(settings.get("antiflood_pause_seconds", 0) or 0)
        await message.reply(
            f"⏳ <b>Анти-флуд «пауза»</b>\n\n"
            f"Текущий интервал: <b>{_format_duration_ru(timedelta(seconds=cur)) if cur else 'выключено'}</b>\n\n"
            f"Установить: <code>антифлуд пауза 5</code> (5 секунд), <code>антифлуд пауза 10м</code>.\n"
            f"Выключить: <code>антифлуд пауза выкл</code>.\n"
            f"<i>Игрок не сможет отправлять сообщения чаще указанного интервала.</i>",
            parse_mode="HTML",
        )


# ===================== НАСТРОЙКА ВАРНОВ (АВТО-ЭСКАЛАЦИЯ) =====================
# (Обработчик находится в social_moderation_handlers, чтобы срабатывать раньше
#  базовой команды "варн".)


# ===================== АНТИМАТ (ФИЛЬТР НЕЦЕНЗУРНОЙ ЛЕКСИКИ) =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "антимат", "!антимат", "/badwords"
])))
async def handle_badwords_settings(message: types.Message, bot: Bot):
    """Настройка фильтра нецензурной лексики (антимат)."""
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    parts = (message.text or "").split()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            return

        from app.services import badwords_service as bw

        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)

        def _persist() -> None:
            chat.settings = settings

        if len(parts) == 1:
            custom = await bw.get_custom_bad_words(session, chat_id=message.chat.id)
            await message.reply(
                f"🤬 <b>Антимат (фильтр мата)</b>\n\n"
                f"Статус: <b>{'Вкл' if settings.get('badwords_enabled') else 'Выкл'}</b>\n"
                f"Действие: <b>{settings.get('badwords_action', 'delete')}</b>\n"
                f"Своих слов в фильтре: <b>{len(custom)}</b>\n\n"
                f"<b>Управление:</b>\n"
                f"• <code>антимат вкл</code> / <code>антимат выкл</code>\n"
                f"• <code>антимат действие удалить|варн|мут</code>\n"
                f"• <code>антимат срок 10м</code> — срок авто-мута\n"
                f"• <code>антимат добавить слово</code> / <code>антимат убрать слово</code>\n"
                f"• <code>антимат список</code> — свои слова",
                parse_mode="HTML",
            )
            return

        cmd = parts[1].casefold()
        if cmd in ("вкл", "включить", "on"):
            settings["badwords_enabled"] = True
            _persist()
            await session.flush()
            await message.reply("🤬 Антимат <b>включён</b>. Сообщения с матом будут удаляться.", parse_mode="HTML")
            return
        if cmd in ("выкл", "отключить", "off"):
            settings["badwords_enabled"] = False
            _persist()
            await session.flush()
            await message.reply("🛑 Антимат <b>выключен</b>.")
            return
        if cmd in ("действие", "action") and len(parts) >= 3:
            act = parts[2].casefold()
            mapping = {"удалить": "delete", "delete": "delete", "варн": "warn", "warn": "warn", "мут": "mute", "mute": "mute"}
            if act not in mapping:
                await message.reply("Укажите: <code>антимат действие удалить|варн|мут</code>", parse_mode="HTML")
                return
            settings["badwords_action"] = mapping[act]
            _persist()
            await session.flush()
            await message.reply(f"⚙️ Действие антимата: <b>{settings['badwords_action']}</b>")
            return
        if cmd in ("срок", "время") and len(parts) >= 3:
            try:
                dur = parse_duration_string(parts[2])
            except Exception as e:
                await message.reply(f"❌ {e}")
                return
            settings["badwords_duration_seconds"] = int(dur.total_seconds())
            _persist()
            await session.flush()
            await message.reply(f"⏱ Срок авто-мута за мат: <b>{_format_duration_ru(dur)}</b>", parse_mode="HTML")
            return
        if cmd in ("добавить", "add") and len(parts) >= 3:
            try:
                word = await bw.add_custom_bad_word(session, chat_id=message.chat.id, word=parts[2])
                await message.reply(f"✅ Слово <code>{word}</code> добавлено в фильтр.", parse_mode="HTML")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return
        if cmd in ("убрать", "удалить", "remove", "del") and len(parts) >= 3:
            ok = await bw.remove_custom_bad_word(session, chat_id=message.chat.id, word=parts[2])
            await message.reply("✅ Слово удалено из фильтра." if ok else "Слово не найдено в фильтре.")
            return
        if cmd in ("список", "list"):
            custom = await bw.get_custom_bad_words(session, chat_id=message.chat.id)
            if not custom:
                await message.reply("Персональный список слов пуст.")
                return
            await message.reply("📝 <b>Свои слова в фильтре:</b>\n" + "\n".join(f"• <code>{w}</code>" for w in custom), parse_mode="HTML")
            return

        await message.reply("Неизвестная настройка антимата. Напишите <code>антимат</code> для справки.", parse_mode="HTML")


# ===================== КАПЧА (ВКЛЮЧЕНИЕ АДМИНОМ) =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "+капча", "капча", "!капча", "/captcha", "включить капчу", "-капча", "выключить капчу"
])))
async def handle_captcha_settings(message: types.Message, bot: Bot):
    """Включение/выключение капчи при входе. Команда «+капча» включает, «-капча» выключает."""
    if not message.chat or message.chat.type == ChatType.PRIVATE:
        return
    text = (message.text or "").strip().casefold()
    parts = text.split()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=bot)
        if lvl < 3:
            return

        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)

        # Явное включение/выключение
        turn_on = text.startswith("+") or parts[0] in ("капча", "включить капчу")
        if len(parts) >= 2:
            if parts[1].casefold() in ("вкл", "включить", "on", "+"):
                turn_on = True
            elif parts[1].casefold() in ("выкл", "отключить", "off", "-"):
                turn_on = False
        if text.startswith("-") or parts[0] == "выключить капчу":
            turn_on = False

        if len(parts) == 1 and not text.startswith(("+", "-")):
            # Показываем статус и справку
            status = "Вкл" if settings.get("captcha_enabled") else "Выкл"
            await message.reply(
                f"🤖 <b>Капча при входе: {status}</b>\n\n"
                f"Капча по умолчанию <b>выключена</b>. Включить: <code>+капча</code>, выключить: <code>-капча</code>.\n"
                f"Новые участники при входе получат кнопку «Я человек»; писать до прохождения капчи нельзя.",
                parse_mode="HTML",
            )
            return

        settings["captcha_enabled"] = turn_on
        chat.settings = settings
        await session.flush()
        if turn_on:
            await message.reply("✅ Капча при входе <b>включена</b>. Новые участники должны нажать кнопку «Я человек».", parse_mode="HTML")
        else:
            await message.reply("🛑 Капча при входе <b>выключена</b>.")
