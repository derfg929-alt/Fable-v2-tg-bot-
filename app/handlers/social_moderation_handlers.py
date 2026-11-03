"""Обработчики кланов, топов, модерации, капчи и правил Fable:
- "клан создать <имя>", "клан", "клан вступить <имя>", "клан чат <текст>", "клан удалить"
- "топ", "топ богатых", "топ активных", "топ кланов" (+ "чат")
- Модерация: "мут", "размут", "варн", "кик", "бан", "разбан", "админ список", "админ назначить", "ссылки"
- Правила: "правила", "правило добавить", "правило удалить", кнопка «Принять правила»
- Капча при входе
- Локальная панель "настройки"
"""
from __future__ import annotations

import re
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.clan_service import (
    create_clan,
    get_player_clan,
    join_clan,
    send_clan_message,
    get_clan_messages,
    delete_clan,
)
from app.services.top_service import get_top_rich, get_top_active, get_top_clans
from app.services.help_service import get_commands_help_text, get_greeting_text
from aiogram.filters import Command
from app.services.moderation_service import (
    get_user_admin_level,
    set_admin_level,
    issue_mute,
    revoke_mute,
    issue_warn,
    revoke_warn,
    get_active_warns,
    record_ban_or_kick,
    is_link_allowed,
    add_link_whitelist,
    remove_link_whitelist,
    list_link_whitelist,
    get_chat_admins,
    parse_duration_string,
    ROLE_NAMES,
)
from app.services.chat_settings_service import (
    get_or_create_chat,
    apply_chat_preset,
    get_chat_rules,
    add_chat_rule,
    remove_chat_rule,
    accept_rules,
    create_captcha,
    solve_captcha,
    create_report,
)
from app.services.activity_service import process_message_activity

router = Router(name="social_moderation_router")


def _get_target_user_id(message: types.Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id
    return None


# ===================== КОМАНДЫ И ПОМОЩЬ =====================

@router.message(Command("команды", "help"))
@router.message(F.text.casefold().in_(["команды", "!команды", "помощь", "!помощь"]))
async def handle_commands_list(message: types.Message):
    """Выводит список всех команд и возможностей бота."""
    await message.reply(get_commands_help_text(), parse_mode="HTML")


# ===================== КЛАНЫ =====================

@router.message(F.text.casefold().startswith("клан"))
async def handle_clan(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)

        if len(parts) == 1:
            info = await get_player_clan(session, user_id=message.from_user.id)
            if not info:
                await message.reply("Вы не состоите в клане. Создайте: <code>клан создать Название</code> или вступите: <code>клан вступить Название</code>", parse_mode="HTML")
                return

            members_list = "\n".join([f"  • {p.first_name} ({r})" for p, r in info.members[:15]])
            resp = (
                f"🛡 <b>КЛАН «{info.clan.name}»</b>\n\n"
                f"👑 Глава: <b>{info.creator_name}</b>\n"
                f"⭐ Уровень: <b>{info.clan.level}</b> | Рейтинг: <b>{info.clan.rating}</b>\n"
                f"👥 Участников: <b>{info.member_count}</b>\n\n"
                f"<b>Состав клана:</b>\n{members_list}"
            )
            await message.reply(resp, parse_mode="HTML")
            return

        cmd = parts[1].casefold()
        if cmd == "создать" and len(parts) >= 3:
            name = " ".join(parts[2:])
            try:
                clan = await create_clan(session, creator_id=message.from_user.id, name=name)
                await message.reply(f"🎉 Клан «{clan.name}» успешно создан! Вы назначены главой.")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        if cmd == "вступить" and len(parts) >= 3:
            name = " ".join(parts[2:])
            try:
                clan = await join_clan(session, user_id=message.from_user.id, clan_name=name)
                await message.reply(f"✅ Вы успешно вступили в клан «{clan.name}»!")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        if cmd == "чат" and len(parts) >= 3:
            text = " ".join(parts[2:])
            try:
                msg = await send_clan_message(session, user_id=message.from_user.id, text=text)
                await message.reply("✉️ Сообщение отправлено в клановый чат.")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        if cmd == "удалить":
            builder = InlineKeyboardBuilder()
            builder.button(text="⚠️ Да, удалить клан", callback_data=f"clan_del_conf_{message.from_user.id}")
            builder.button(text="❌ Отмена", callback_data="clan_del_cancel")
            builder.adjust(2)
            await message.reply("Ты действительно хочешь удалить клан? Это действие необратимо!", reply_markup=builder.as_markup())
            return


@router.callback_query(F.data.startswith("clan_del_conf_"))
async def on_clan_delete_confirm(callback: types.CallbackQuery):
    creator_id = int(callback.data.split("_")[3])
    if callback.from_user.id != creator_id:
        await callback.answer("Только создатель может подтвердить удаление клана!", show_alert=True)
        return
    async with session_scope() as session:
        try:
            name = await delete_clan(session, user_id=callback.from_user.id)
            await callback.message.edit_text(f"💥 Клан «{name}» был успешно распущен.")
        except Exception as e:
            await callback.answer(f"❌ {e}", show_alert=True)


@router.callback_query(F.data == "clan_del_cancel")
async def on_clan_delete_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Отмена удаления клана.")


# ===================== ТОПЫ И РЕЙТИНГИ =====================

@router.message(F.text.casefold().startswith("топ"))
async def handle_tops(message: types.Message):
    if not message.from_user:
        return
    text = (message.text or "").strip().casefold()
    is_chat = "чат" in text
    chat_id = message.chat.id if is_chat and message.chat.type != types.ChatType.PRIVATE else None

    async with session_scope() as session:
        if text in ("топ", "!топ"):
            builder = InlineKeyboardBuilder()
            builder.button(text="💰 Богатые", callback_data="top_menu_rich")
            builder.button(text="🔥 Активные", callback_data="top_menu_active")
            builder.button(text="🛡 Кланы", callback_data="top_menu_clans")
            builder.adjust(3)
            await message.reply("🏆 <b>Выберите рейтинг Fable:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
            return

        if "богат" in text:
            rich = await get_top_rich(session, chat_id=chat_id, limit=10)
            lines = [f"💰 <b>Топ богатых ({'чата' if chat_id else 'глобальный'}):</b>\n"]
            for r in rich:
                lines.append(f"{r.rank}. <b>{r.name}</b> — {r.total_wealth} монет")
            await message.reply("\n".join(lines) if rich else "Рейтинг пуст.", parse_mode="HTML")
            return

        if "актив" in text:
            active = await get_top_active(session, chat_id=chat_id, limit=10)
            lines = [f"🔥 <b>Топ активных ({'чата' if chat_id else 'глобальный'}):</b>\n"]
            for a in active:
                lines.append(f"{a.rank}. <b>{a.name}</b> — {a.activity_points} очков")
            await message.reply("\n".join(lines) if active else "Рейтинг пуст.", parse_mode="HTML")
            return

        if "клан" in text:
            clans = await get_top_clans(session, chat_id=chat_id, limit=10)
            lines = [f"🛡 <b>Топ кланов ({'чата' if chat_id else 'глобальный'}):</b>\n"]
            for c in clans:
                lines.append(f"{c.rank}. <b>«{c.name}»</b> — рейтинг {c.rating} (ур. {c.level})")
            await message.reply("\n".join(lines) if clans else "Рейтинг пуст.", parse_mode="HTML")
            return


# ===================== ПРАВИЛА И МОДЕРАЦИЯ =====================

@router.message(F.text.casefold().in_(["правила", "!правила"]))
async def handle_rules(message: types.Message):
    if not message.chat or message.chat.type in (types.ChatType.PRIVATE,):
        await message.reply("Правила настраиваются отдельно в каждой группе.")
        return

    async with session_scope() as session:
        rules = await get_chat_rules(session, chat_id=message.chat.id)
        if not rules:
            await message.reply("📜 В этом чате пока не установлены правила.")
            return

        lines = [f"📜 <b>Правила чата «{message.chat.title}»:</b>\n"]
        for r in rules:
            lines.append(f"<b>{r.position}.</b> {r.text}")

        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Принять правила", callback_data="rules_accept")
        await message.reply("\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "rules_accept")
async def on_rules_accept(callback: types.CallbackQuery):
    async with session_scope() as session:
        await accept_rules(session, chat_id=callback.message.chat.id, user_id=callback.from_user.id)
        await callback.answer("✅ Вы успешно приняли правила чата!", show_alert=True)


@router.callback_query(F.data.startswith("captcha_solve_"))
async def on_captcha_solve(callback: types.CallbackQuery):
    token = callback.data.split("_")[2]
    async with session_scope() as session:
        ok = await solve_captcha(session, challenge_token=token, actor_id=callback.from_user.id)
        if ok:
            await callback.answer("✅ Капча пройдена!", show_alert=True)
            await callback.message.delete()
        else:
            await callback.answer("❌ Это не ваша капча или время истекло!", show_alert=True)


@router.message(F.text.casefold().startswith("мут"))
async def handle_mute(message: types.Message):
    if not message.chat or message.chat.type == types.ChatType.PRIVATE:
        return
    parts = (message.text or "").split()
    target_id = _get_target_user_id(message)
    if not target_id or len(parts) < 2:
        await message.reply("Использование: ответом на сообщение <code>мут 30м [причина]</code>", parse_mode="HTML")
        return

    dur_str = parts[1] if parts[1] != f"@{target_id}" else (parts[2] if len(parts) > 2 else "30м")
    reason = " ".join(parts[2:]) if len(parts) > 2 else "Нарушение порядка"

    async with session_scope() as session:
        try:
            dur = parse_duration_string(dur_str)
            await issue_mute(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, duration=dur, reason=reason)
            await message.reply(f"🔇 Пользователь ID <code>{target_id}</code> замьючен на {dur_str}. Причина: {reason}", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("размут"))
async def handle_unmute(message: types.Message):
    target_id = _get_target_user_id(message)
    if not target_id:
        return
    async with session_scope() as session:
        try:
            await revoke_mute(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id)
            await message.reply(f"🔊 Пользователь ID <code>{target_id}</code> размьючен.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("варн"))
async def handle_warn(message: types.Message):
    target_id = _get_target_user_id(message)
    if not target_id:
        return
    text = (message.text or "").strip()
    parts = text.split()

    async with session_scope() as session:
        if "снять" in text:
            ok = await revoke_warn(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id)
            await message.reply("✅ Предупреждение снято." if ok else "У пользователя нет активных предупреждений.")
            return

        reason = " ".join(parts[1:]) if len(parts) > 1 else "Нарушение"
        try:
            case, cnt = await issue_warn(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, reason=reason)
            await message.reply(f"⚠️ Пользователю ID <code>{target_id}</code> выдано предупреждение ({cnt}/5). Причина: {reason}", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().in_(["админ список", "!админ список"]))
async def handle_admin_list(message: types.Message):
    if not message.chat or message.chat.type == types.ChatType.PRIVATE:
        return
    async with session_scope() as session:
        admins = await get_chat_admins(session, chat_id=message.chat.id)
        if not admins:
            await message.reply("В чате еще не назначены администраторы Fable.")
            return
        lines = [f"🛡 <b>Администрация чата «{message.chat.title}»:</b>\n"]
        for p, lvl, role in admins:
            lines.append(f"• <b>{p.first_name}</b> — Уровень {lvl} ({role})")
        await message.reply("\n".join(lines), parse_mode="HTML")
