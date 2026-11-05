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
from aiogram.enums import ChatType
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


async def _get_target_user_id(message: types.Message, session) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id
    text = message.text or ""
    match = re.search(r"@([a-zA-Z0-9_]+)", text)
    if match:
        uname = match.group(1).lower()
        from app.db.models import Player
        from sqlalchemy import select, func
        p = await session.scalar(select(Player).where(func.lower(Player.username) == uname))
        if p:
            return p.telegram_id
    parts = text.split()
    for part in parts[1:]:
        if part.isdigit() and len(part) >= 6:
            return int(part)
    return None


# ===================== КОМАНДЫ И ПОМОЩЬ =====================

def _build_help_sections_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="1⃣ Основное", callback_data="help_sec_1")
    builder.button(text="2⃣ Игры", callback_data="help_sec_2")
    builder.button(text="3⃣ Заработок", callback_data="help_sec_3")
    builder.button(text="4⃣ Кланы", callback_data="help_sec_4")
    builder.button(text="5⃣ Города", callback_data="help_sec_5")
    builder.button(text="6⃣ Развлечения", callback_data="help_sec_6")
    builder.button(text="7⃣ Бизнесы", callback_data="help_sec_7")
    builder.button(text="8⃣ Персонаж", callback_data="help_sec_8")
    builder.button(text="9⃣ Космодром", callback_data="help_sec_9")
    builder.button(text="🔟 Биржа", callback_data="help_sec_10")
    builder.adjust(2, 2, 2, 2, 2)
    return builder.as_markup()


@router.message(Command("команды", "help"))
@router.message(F.text.casefold().in_(["команды", "!команды", "помощь", "!помощь"]))
@router.message(F.text.casefold().startswith("помощь"))
async def handle_commands_list(message: types.Message):
    """Выводит список всех команд и разделов помощи бота."""
    msg_text = getattr(message, "text", None) or ""
    parts = msg_text.strip().split()
    if len(parts) > 1:
        arg = parts[1].strip()
        from app.services.help_service import get_section_help_text
        if arg.isdigit():
            sec_num = int(arg)
            text = get_section_help_text(sec_num)
            if text:
                builder = InlineKeyboardBuilder()
                builder.button(text="◀️ Все разделы", callback_data="help_sec_all")
                await message.reply(text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return

    await message.reply(
        get_commands_help_text(),
        reply_markup=_build_help_sections_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("help_sec_"))
async def on_help_section_click(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    sec_str = callback.data.split("_")[2]
    if sec_str == "all":
        try:
            await callback.message.edit_text(
                get_commands_help_text(),
                reply_markup=_build_help_sections_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    sec_id = int(sec_str)
    from app.services.help_service import get_section_help_text
    text = get_section_help_text(sec_id)
    if text:
        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ В меню разделов", callback_data="help_sec_all")
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            pass


@router.message(F.text.casefold().in_(["реф", "!реф"]))
@router.message(F.text.casefold().startswith("реф"))
async def handle_ref_system(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").strip().split()
    user_id = message.from_user.id

    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=user_id, first_name=message.from_user.first_name)
        player = reg.player

        if len(parts) >= 2 and parts[1].isdigit():
            ref_id = int(parts[1])
            if ref_id == user_id:
                await message.reply("❌ Вы не можете пригласить самого себя!")
                return
            if player.referred_by:
                await message.reply("❌ Вы уже активировали реферальный код ранее!")
                return

            from app.db.models import Player
            inviter = await session.get(Player, ref_id)
            if not inviter:
                await message.reply("❌ Пригласивший игрок не найден в системе Fable!")
                return

            player.referred_by = ref_id
            from app.services.economy_service import credit_coins
            bonus = 1_000
            await credit_coins(session, user_id=user_id, amount=bonus, reason=f"Бонус за реферала от {ref_id}", operation_type="referral_bonus")
            await credit_coins(session, user_id=ref_id, amount=bonus, reason=f"Бонус за приглашение игрока {user_id}", operation_type="referral_bonus")
            await message.reply(f"🎉 <b>Реферальный код активирован!</b>\n\nВы и ваш друг получили по <b>+{bonus:,}</b> монет!".replace(",", " "), parse_mode="HTML")
            return

        bot_info = await message.bot.get_me()
        bot_username = bot_info.username or "FableBot"
        ref_link = f"https://t.me/{bot_username}?start={user_id}"

        resp = (
            f"✉️ <b>РЕФЕРАЛЬНАЯ СИСТЕМА FABLE</b>\n\n"
            f"Приглашайте друзей и развивайтесь в игре вместе!\n"
            f"🎁 <b>Награда:</b> по <b>1 000 монет</b> вам и каждому приглашенному игроку!\n\n"
            f"🔗 Ваша ссылка для приглашения:\n<code>{ref_link}</code>\n\n"
            f"Или друг может отправить в чат бота команду:\n"
            f"<code>реф {user_id}</code>"
        )
        await message.reply(resp, parse_mode="HTML")


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
    try:
        await callback.answer("Удаление клана отменено.")
    except Exception:
        pass
    try:
        await callback.message.edit_text("Отмена удаления клана.")
    except Exception:
        pass


# ===================== ТОПЫ И РЕЙТИНГИ =====================

def _build_tops_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Богатые", callback_data="top_menu_rich")
    builder.button(text="🔥 Активные", callback_data="top_menu_active")
    builder.button(text="🛡 Кланы", callback_data="top_menu_clans")
    builder.adjust(3)
    return builder.as_markup()


def _build_top_category_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Богатые", callback_data="top_menu_rich")
    builder.button(text="🔥 Активные", callback_data="top_menu_active")
    builder.button(text="🛡 Кланы", callback_data="top_menu_clans")
    builder.button(text="◀️ В меню топов", callback_data="top_menu_back")
    builder.adjust(3, 1)
    return builder.as_markup()


@router.message(F.text.casefold().startswith("топ"))
async def handle_tops(message: types.Message):
    if not message.from_user:
        return
    text = (message.text or "").strip().casefold()
    # Определяем контекст топа: чат или глобальный
    is_chat = "чат" in text
    chat_id = message.chat.id if (is_chat or text in ("топ чат", "!топ чат")) and message.chat.type != types.ChatType.PRIVATE else None

    async with session_scope() as session:
        if text in ("топ", "!топ", "топ глобальный", "!топ глобальный", "топ чат", "!топ чат"):
            scope_str = "чата" if chat_id else "глобальный"
            await message.reply(
                f"🏆 <b>Выберите рейтинг Fable ({scope_str}):</b>\n\n"
                f"💡 <i>Доступны команды:</i>\n"
                f"• <code>топ богатых</code> / <code>топ богатых чат</code>\n"
                f"• <code>топ активных</code> / <code>топ активных чат</code>\n"
                f"• <code>топ кланов</code> / <code>топ кланов чат</code>",
                reply_markup=_build_tops_main_keyboard(),
                parse_mode="HTML",
            )
            return

        if "богат" in text:
            rich = await get_top_rich(session, chat_id=chat_id, limit=10)
            scope = f"чата «{message.chat.title}»" if chat_id and message.chat else "глобальный"
            lines = [f"💰 <b>Топ богатых ({scope}):</b>\n"]
            for r in rich:
                lines.append(f"{r.rank}. <b>{r.name}</b> — {r.total_wealth:,} монет".replace(",", " "))
            await message.reply("\n".join(lines) if rich else f"💰 <b>Топ богатых ({scope}):</b>\nРейтинг пока пуст.", parse_mode="HTML")
            return

        if "актив" in text:
            active = await get_top_active(session, chat_id=chat_id, limit=10)
            scope = f"чата «{message.chat.title}»" if chat_id and message.chat else "глобальный"
            lines = [f"🔥 <b>Топ активных ({scope}):</b>\n"]
            for a in active:
                lines.append(f"{a.rank}. <b>{a.name}</b> — {a.activity_points:,} очков".replace(",", " "))
            await message.reply("\n".join(lines) if active else f"🔥 <b>Топ активных ({scope}):</b>\nРейтинг пока пуст.", parse_mode="HTML")
            return

        if "клан" in text:
            clans = await get_top_clans(session, chat_id=chat_id, limit=10)
            scope = f"чата «{message.chat.title}»" if chat_id and message.chat else "глобальный"
            lines = [f"🛡 <b>Топ кланов ({scope}):</b>\n"]
            for c in clans:
                lines.append(f"{c.rank}. <b>«{c.name}»</b> — рейтинг {c.rating} (ур. {c.level})")
            await message.reply("\n".join(lines) if clans else f"🛡 <b>Топ кланов ({scope}):</b>\nРейтинг пока пуст.", parse_mode="HTML")
            return


@router.callback_query(F.data == "top_menu_back")
async def on_top_menu_back(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text(
            "🏆 <b>Выберите рейтинг Fable:</b>",
            reply_markup=_build_tops_main_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "top_menu_rich")
async def on_top_menu_rich(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    chat_id = callback.message.chat.id if callback.message.chat.type != ChatType.PRIVATE else None
    async with session_scope() as session:
        rich = await get_top_rich(session, chat_id=chat_id, limit=10)
        lines = [f"💰 <b>Топ богатых ({'чата' if chat_id else 'глобальный'}):</b>\n"]
        for r in rich:
            lines.append(f"{r.rank}. <b>{r.name}</b> — {r.total_wealth:,} монет".replace(",", " "))
        text = "\n".join(lines) if rich else "💰 <b>Топ богатых:</b>\nРейтинг пуст."
        try:
            await callback.message.edit_text(
                text,
                reply_markup=_build_top_category_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data == "top_menu_active")
async def on_top_menu_active(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    chat_id = callback.message.chat.id if callback.message.chat.type != ChatType.PRIVATE else None
    async with session_scope() as session:
        active = await get_top_active(session, chat_id=chat_id, limit=10)
        lines = [f"🔥 <b>Топ активных ({'чата' if chat_id else 'глобальный'}):</b>\n"]
        for a in active:
            lines.append(f"{a.rank}. <b>{a.name}</b> — {a.activity_points:,} очков".replace(",", " "))
        text = "\n".join(lines) if active else "🔥 <b>Топ активных:</b>\nРейтинг пуст."
        try:
            await callback.message.edit_text(
                text,
                reply_markup=_build_top_category_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(F.data == "top_menu_clans")
async def on_top_menu_clans(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    chat_id = callback.message.chat.id if callback.message.chat.type != ChatType.PRIVATE else None
    async with session_scope() as session:
        clans = await get_top_clans(session, chat_id=chat_id, limit=10)
        lines = [f"🛡 <b>Топ кланов ({'чата' if chat_id else 'глобальный'}):</b>\n"]
        for c in clans:
            lines.append(f"{c.rank}. <b>«{c.name}»</b> — рейтинг {c.rating} (ур. {c.level})")
        text = "\n".join(lines) if clans else "🛡 <b>Топ кланов:</b>\nРейтинг пуст."
        try:
            await callback.message.edit_text(
                text,
                reply_markup=_build_top_category_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            pass


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

    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id or len(parts) < 2:
            await message.reply("Использование: ответом на сообщение <code>мут 30м [причина]</code> или <code>мут @user 30м [причина]</code>", parse_mode="HTML")
            return

        # Find duration
        dur_str = "30м"
        reason_parts = []
        for p in parts[1:]:
            if re.match(r"^(\d+)([мчдmhd])$", p.lower()):
                dur_str = p
            elif not p.startswith("@") and not (p.isdigit() and int(p) == target_id):
                reason_parts.append(p)
        reason = " ".join(reason_parts) or "Нарушение порядка"

        try:
            dur = parse_duration_string(dur_str)
            case = await issue_mute(
                session,
                chat_id=message.chat.id,
                moderator_id=message.from_user.id,
                target_id=target_id,
                duration=dur,
                reason=reason,
                bot=message.bot,
            )
            # Применение мута в Telegram через ChatPermissions
            tg_status = ""
            if message.bot:
                try:
                    from aiogram.types import ChatPermissions
                    no_permissions = ChatPermissions(
                        can_send_messages=False,
                        can_send_media_messages=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False,
                    )
                    await message.bot.restrict_chat_member(
                        chat_id=message.chat.id,
                        user_id=target_id,
                        permissions=no_permissions,
                        until_date=case.expires_at,
                    )
                    tg_status = " <i>(в Telegram права ограничены)</i>"
                except Exception as tg_err:
                    tg_status = f"\n⚠️ <i>Примечание: бот не смог ограничить участника в Telegram ({tg_err}). Убедитесь, что бот является администратором с правом блокировки пользователей!</i>"

            await message.reply(
                f"🔇 <b>ПОЛЬЗОВАТЕЛЬ ЗАМЬЮЧЕН!</b>\n\n"
                f"👤 Нарушитель: ID <code>{target_id}</code>\n"
                f"⏱ Срок мута: <b>{dur_str}</b>\n"
                f"📝 Причина: <i>{reason}</i>\n"
                f"👮‍♂️ Модератор: ID <code>{message.from_user.id}</code>{tg_status}",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("размут"))
async def handle_unmute(message: types.Message):
    if not message.chat or message.chat.type == types.ChatType.PRIVATE:
        return
    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>размут</code> или <code>размут @user</code>", parse_mode="HTML")
            return
        try:
            await revoke_mute(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, bot=message.bot)
            tg_status = ""
            if message.bot:
                try:
                    from aiogram.types import ChatPermissions
                    full_permissions = ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                    )
                    await message.bot.restrict_chat_member(
                        chat_id=message.chat.id,
                        user_id=target_id,
                        permissions=full_permissions,
                    )
                    tg_status = " <i>(в Telegram ограничения сняты)</i>"
                except Exception as tg_err:
                    tg_status = f"\n⚠️ <i>Примечание: ограничение в Telegram снять не удалось ({tg_err}).</i>"

            await message.reply(
                f"🔊 <b>МУТ СНЯТ!</b>\n\n"
                f"👤 Пользователь ID <code>{target_id}</code> успешно размьючен.{tg_status}",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("варн"))
async def handle_warn(message: types.Message):
    text = (message.text or "").strip()
    parts = text.split()

    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>варн [причина]</code> или <code>варн @user [причина]</code>", parse_mode="HTML")
            return

        if "снять" in text:
            try:
                ok = await revoke_warn(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id)
                await message.reply("✅ Предупреждение снято." if ok else "У пользователя нет активных предупреждений.")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        reason = " ".join([p for p in parts[1:] if not p.startswith("@") and p != str(target_id)]) or "Нарушение"
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
