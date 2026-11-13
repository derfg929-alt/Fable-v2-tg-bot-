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
from app.services.top_service import (
    get_top_rich,
    get_top_active,
    get_top_clans,
    get_top_level,
    get_top_rating,
    get_top_mine,
    get_top_businesses,
    get_top_cosmodrome,
)
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
    apply_warn_escalation,
    record_ban_or_kick,
    is_link_allowed,
    add_link_whitelist,
    remove_link_whitelist,
    list_link_whitelist,
    get_chat_admins,
    parse_duration_string,
    extract_moderation_args,
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
    builder.button(text="🛡 Модерация", callback_data="help_sec_11")
    builder.adjust(2, 2, 2, 2, 2, 1)
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
        from app.services.help_service import get_section_help_text, MODERATION_HELP_TEXT
        if arg.isdigit():
            sec_num = int(arg)
            text = get_section_help_text(sec_num)
            if text:
                builder = InlineKeyboardBuilder()
                builder.button(text="◀️ Все разделы", callback_data="help_sec_all")
                await message.reply(text, reply_markup=builder.as_markup(), parse_mode="HTML")
                return
        if arg.casefold() in ("модерация", "модерации", "moderation"):
            await message.reply(MODERATION_HELP_TEXT, parse_mode="HTML")
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
        try:
            await callback.answer("Только создатель может подтвердить удаление клана!", show_alert=True)
        except Exception:
            pass
        return
    async with session_scope() as session:
        try:
            name = await delete_clan(session, user_id=callback.from_user.id)
            try:
                await callback.answer("Клан удален!")
            except Exception:
                pass
            try:
                await callback.message.edit_text(f"💥 Клан «{name}» был успешно распущен.")
            except Exception:
                pass
        except Exception as e:
            try:
                await callback.answer(f"❌ {e}", show_alert=True)
            except Exception:
                pass


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

    async with session_scope() as session:
        if text in ("топ", "!топ", "топ глобальный", "!топ глобальный", "топ чат", "!топ чат"):
            await message.reply(
                f"🏆 <b>Глобальные рейтинги игроков Fable:</b>\n\n"
                f"💡 <i>Доступные категории:</i>\n"
                f"• <code>топ богатых</code> — богатейшие игроки по балансу\n"
                f"• <code>топ активных</code> — лидеры по активности в сообщениях\n"
                f"• <code>топ кланов</code> — сильнейшие кланы по рейтингу\n\n"
                f"<i>Все рейтинги отображают лучших игроков всего проекта!</i>",
                reply_markup=_build_tops_main_keyboard(),
                parse_mode="HTML",
            )
            return

        if any(w in text for w in ("богат", "баланс", "денег", "кошельк")):
            rich = await get_top_rich(session, limit=10)
            lines = ["💰 <b>Глобальный топ богатых игроков:</b>\n"]
            for r in rich:
                lines.append(f"{r.rank}. <b>{r.name}</b> — {r.total_wealth:,} монет".replace(",", " "))
            await message.reply("\n".join(lines) if rich else "💰 <b>Глобальный топ богатых:</b>\nРейтинг пока пуст.", parse_mode="HTML")
            return

        if any(w in text for w in ("актив", "сообщени")):
            active = await get_top_active(session, limit=10)
            lines = ["🔥 <b>Глобальный топ активных игроков:</b>\n"]
            for a in active:
                lines.append(f"{a.rank}. <b>{a.name}</b> — {a.activity_points:,} очков".replace(",", " "))
            await message.reply("\n".join(lines) if active else "🔥 <b>Глобальный топ активных:</b>\nРейтинг пока пуст.", parse_mode="HTML")
            return

        if "клан" in text:
            clans = await get_top_clans(session, limit=10)
            lines = ["🛡 <b>Глобальный топ кланов:</b>\n"]
            for c in clans:
                lines.append(f"{c.rank}. <b>«{c.name}»</b> — рейтинг {c.rating} (ур. {c.level})")
            await message.reply("\n".join(lines) if clans else "🛡 <b>Глобальный топ кланов:</b>\nРейтинг пока пуст.", parse_mode="HTML")
            return

        # Если введено что-то вроде "топ рейтинг", "топ уровней" и т.д. — подсказываем 3 актуальных топа
        await message.reply(
            "🏆 <b>В игре действуют 3 основных глобальных топа:</b>\n\n"
            "• <code>топ богатых</code> — лидеры по богатству и капиталу\n"
            "• <code>топ активных</code> — лидеры по активности сообщений\n"
            "• <code>топ кланов</code> — сильнейшие кланы\n\n"
            "<i>Выберите нужный рейтинг по кнопке ниже:</i>",
            reply_markup=_build_tops_main_keyboard(),
            parse_mode="HTML",
        )


@router.callback_query(F.data == "top_menu_back")
async def on_top_menu_back(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text(
            "🏆 <b>Выберите рейтинг Fable (глобальный):</b>\n\n"
            "• <code>топ богатых</code>\n"
            "• <code>топ активных</code>\n"
            "• <code>топ кланов</code>",
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
    async with session_scope() as session:
        rich = await get_top_rich(session, limit=10)
        lines = ["💰 <b>Топ богатых (глобальный):</b>\n"]
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
    async with session_scope() as session:
        active = await get_top_active(session, limit=10)
        lines = ["🔥 <b>Топ активных (глобальный):</b>\n"]
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
    async with session_scope() as session:
        clans = await get_top_clans(session, limit=10)
        lines = ["🛡 <b>Топ кланов (глобальный):</b>\n"]
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


# Устаревшие колбэки для плавного перехода
@router.callback_query(F.data.in_(["top_menu_level", "top_menu_rating", "top_menu_mine", "top_menu_biz", "top_menu_cosmo"]))
async def on_legacy_top_menu_click(callback: types.CallbackQuery):
    try:
        await callback.answer("Рейтинг обновлен: теперь доступны топ богатых, топ активных и топ кланов!")
    except Exception:
        pass
    try:
        await callback.message.edit_text(
            "🏆 <b>Глобальные рейтинги игроков Fable:</b>\n\n"
            "• <code>топ богатых</code>\n"
            "• <code>топ активных</code>\n"
            "• <code>топ кланов</code>",
            reply_markup=_build_tops_main_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        pass


# ===================== ПРАВИЛА И МОДЕРАЦИЯ =====================

@router.message(F.text.casefold().in_(["правила", "!правила"]))
async def handle_rules(message: types.Message):
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
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
        try:
            await callback.answer("✅ Вы успешно приняли правила чата!", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("captcha_solve_"))
async def on_captcha_solve(callback: types.CallbackQuery):
    token = callback.data.split("_")[2]
    async with session_scope() as session:
        ok = await solve_captcha(session, challenge_token=token, actor_id=callback.from_user.id)
        if ok:
            try:
                await callback.answer("✅ Капча пройдена!", show_alert=True)
            except Exception:
                pass
            try:
                await callback.message.delete()
            except Exception:
                pass
        else:
            try:
                await callback.answer("❌ Это не ваша капча или время истекло!", show_alert=True)
            except Exception:
                pass


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in ["мут", "!мут", "/mute", "замутить"])))
async def handle_mute(message: types.Message):
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    raw_text = (message.text or "").strip()
    parts = raw_text.split()

    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id or (len(parts) < 2 and not message.reply_to_message):
            await message.reply("Использование: ответом на сообщение <code>мут 30м [причина]</code> или <code>мут @user 1 час 30 [причина]</code>", parse_mode="HTML")
            return

        from app.services.moderation_service import ensure_player_and_chat_exist
        await ensure_player_and_chat_exist(
            session,
            chat_id=message.chat.id,
            user_ids=[message.from_user.id, target_id],
            bot=message.bot,
        )

        from datetime import timedelta
        dur, dur_str, reason = extract_moderation_args(
            full_text=raw_text,
            target_id=target_id,
            default_duration=timedelta(minutes=30),
        )

        try:
            if dur is None or dur.total_seconds() <= 0:
                dur = timedelta(days=365)
                dur_str = "навсегда"

            case = await issue_mute(
                session,
                chat_id=message.chat.id,
                moderator_id=message.from_user.id,
                target_id=target_id,
                duration=dur,
                reason=reason,
                bot=message.bot,
            )
            from app.services.logging_service import notify_moderation_log
            await notify_moderation_log(
                session,
                bot=message.bot,
                chat_id=message.chat.id,
                text=(
                    f"🔇 <b>МУТ</b>\nЦель: <code>{target_id}</code>\n"
                    f"Срок: <b>{dur_str}</b>\nПричина: {reason}\n"
                    f"Модератор: <code>{message.from_user.id}</code>"
                ),
            )
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


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in ["размут", "!размут", "/unmute", "снять мут"])))
async def handle_unmute(message: types.Message):
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>размут</code> или <code>размут @user</code>", parse_mode="HTML")
            return
        from app.services.moderation_service import ensure_player_and_chat_exist
        await ensure_player_and_chat_exist(
            session,
            chat_id=message.chat.id,
            user_ids=[message.from_user.id, target_id],
            bot=message.bot,
        )
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


@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "варн настройки", "варн лимит", "варн действие", "варн срок", "варн эскалация",
    "!варн настройки", "настройки варнов",
])))
async def handle_warn_settings(message: types.Message):
    """Настройка лимита варнов и авто-наказания при достижении лимита."""
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    from datetime import timedelta
    from app.services.moderation_service import _format_duration_ru
    from app.services.chat_settings_service import DEFAULT_CHAT_SETTINGS

    parts = (message.text or "").split()
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=message.bot)
        if lvl < 3:
            return

        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)

        def _persist() -> None:
            chat.settings = settings

        def _duration_str(seconds: int) -> str:
            if seconds <= 0:
                return "навсегда (для бана)"
            return _format_duration_ru(timedelta(seconds=seconds))

        cmd = parts[1].casefold() if len(parts) > 1 else ""
        if len(parts) == 1 or cmd in ("настройки", "статус", "status"):
            limit = int(settings.get("warn_limit", 5) or 0)
            action = settings.get("warn_action", "mute")
            dur = int(settings.get("warn_duration_seconds", 3600) or 0)
            await message.reply(
                f"⚠️ <b>Настройки предупреждений (варнов)</b>\n\n"
                f"Лимит до наказания: <b>{limit or 'отключено'}</b>\n"
                f"Действие при лимите: <b>{action}</b>\n"
                f"Срок наказания: <b>{_duration_str(dur)}</b>\n"
                f"Эскалация в бессрочный бан на двойном лимите: <b>{'Да' if settings.get('warn_escalate_ban', True) else 'Нет'}</b>\n\n"
                f"<b>Управление:</b>\n"
                f"• <code>варн лимит 5</code> — сколько варнов до авто-наказания (0 — выключить)\n"
                f"• <code>варн действие мут|бан|кик|нет</code>\n"
                f"• <code>варн срок 1ч 30м</code> — срок авто-наказания\n"
                f"• <code>варн эскалация вкл|выкл</code> — бессрочный бан на двойном лимите",
                parse_mode="HTML",
            )
            return
        if cmd in ("лимит", "limit") and len(parts) >= 3 and parts[2].isdigit():
            settings["warn_limit"] = max(0, int(parts[2]))
            _persist()
            await session.flush()
            await message.reply(f"⚠️ Лимит варнов установлен: <b>{settings['warn_limit'] or 'отключено'}</b>", parse_mode="HTML")
            return
        if cmd in ("действие", "action") and len(parts) >= 3:
            act = parts[2].casefold()
            mapping = {"мут": "mute", "mute": "mute", "бан": "ban", "ban": "ban", "кик": "kick", "kick": "kick", "нет": "none", "none": "none"}
            if act not in mapping:
                await message.reply("Укажите: <code>варн действие мут</code>, <code>варн действие бан</code>, <code>варн действие кик</code> или <code>варн действие нет</code>", parse_mode="HTML")
                return
            settings["warn_action"] = mapping[act]
            _persist()
            await session.flush()
            await message.reply(f"⚙️ Действие при лимите варнов: <b>{settings['warn_action']}</b>")
            return
        if cmd in ("срок", "время", "duration") and len(parts) >= 3:
            try:
                dur = parse_duration_string(" ".join(parts[2:]))
            except Exception as e:
                await message.reply(f"❌ {e}")
                return
            settings["warn_duration_seconds"] = int(dur.total_seconds())
            _persist()
            await session.flush()
            await message.reply(f"⏱ Срок авто-наказания по варнам: <b>{_duration_str(settings['warn_duration_seconds'])}</b>", parse_mode="HTML")
            return
        if cmd in ("эскалация", "escalate") and len(parts) >= 3:
            val = parts[2].casefold() in ("вкл", "включить", "on", "да", "yes", "true")
            settings["warn_escalate_ban"] = val
            _persist()
            await session.flush()
            await message.reply(f"⚙️ Эскалация в бессрочный бан на двойном лимите: <b>{'Вкл' if val else 'Выкл'}</b>")
            return

        await message.reply("Неизвестная настройка. Напишите <code>варн настройки</code> для справки.", parse_mode="HTML")


@router.message(F.text.casefold().func(lambda t: any(t == prefix or t.startswith(f"{prefix} ") for prefix in ["варны", "!варны", "/warns", "преды", "!преды"])))
async def handle_view_warns(message: types.Message):
    """Просмотр счётчика и активных предупреждений пользователя в чате."""
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id and message.from_user:
            target_id = message.from_user.id
        if not target_id:
            return

        warns = await get_active_warns(session, chat_id=message.chat.id, target_id=target_id)
        chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
        warn_limit = int((chat.settings or {}).get("warn_limit", 5) or 0)
        limit_str = warn_limit or "∞"
        chat_title = message.chat.title or "этом чате"
        if not warns:
            await message.reply(
                f"✅ <b>Счётчик варнов: 0/{limit_str}</b>\n\n"
                f"У пользователя ID <code>{target_id}</code> нет активных предупреждений в «{chat_title}».",
                parse_mode="HTML",
            )
            return

        lines = [
            f"⚠️ <b>Счётчик варнов: {len(warns)}/{limit_str}</b>\n"
            f"Предупреждения пользователя ID <code>{target_id}</code> в «{chat_title}»:\n"
        ]
        for idx, w in enumerate(warns, start=1):
            date_str = w.created_at.strftime("%d.%m.%Y %H:%M") if w.created_at else ""
            lines.append(f"{idx}. <b>{w.reason}</b> <i>({date_str})</i> — от модератора ID {w.moderator_id}")
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in [
    "варн", "!варн", "пред", "!пред", "предупреждение", "!предупреждение",
    "снять варн", "снять пред", "разварн", "!разварн"
])))
async def handle_warn(message: types.Message):
    text = (message.text or "").strip()
    parts = text.split()

    async with session_scope() as session:
        target_id = await _get_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: ответом на сообщение <code>варн [причина]</code> или <code>варн @user [причина]</code>", parse_mode="HTML")
            return

        from app.services.moderation_service import ensure_player_and_chat_exist
        await ensure_player_and_chat_exist(
            session,
            chat_id=message.chat.id,
            user_ids=[message.from_user.id, target_id],
            bot=message.bot,
        )

        if any(w in text.casefold() for w in ("снять", "разварн", "unwarn")):
            try:
                ok = await revoke_warn(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, bot=message.bot)
                await message.reply("✅ Предупреждение снято." if ok else "У пользователя нет активных предупреждений.")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        reason = " ".join([p for p in parts[1:] if not p.startswith("@") and p != str(target_id)]) or "Нарушение"
        try:
            case, cnt = await issue_warn(session, chat_id=message.chat.id, moderator_id=message.from_user.id, target_id=target_id, reason=reason, bot=message.bot)
            chat = await get_or_create_chat(session, chat_id=message.chat.id, title=message.chat.title or "")
            settings = chat.settings or {}
            warn_limit = int(settings.get("warn_limit", 5) or 0)
            from app.services.logging_service import notify_moderation_log
            await notify_moderation_log(
                session,
                bot=message.bot,
                chat_id=message.chat.id,
                text=(
                    f"⚠️ <b>ВАРН</b> ({cnt}/{warn_limit or '∞'})\nЦель: <code>{target_id}</code>\n"
                    f"Причина: {reason}\nМодератор: <code>{message.from_user.id}</code>"
                ),
            )

            # Авто-эскалация: при достижении лимита варнов выдаём настроенное наказание.
            escalation_text = ""
            try:
                esc = await apply_warn_escalation(
                    session,
                    chat_id=message.chat.id,
                    target_id=target_id,
                    warn_count=cnt,
                    settings=settings,
                    bot=message.bot,
                )
                if esc.triggered:
                    escalation_text = "\n\n" + esc.message
                    # Применяем ограничение/бан в самом Telegram.
                    if message.bot:
                        from aiogram.types import ChatPermissions
                        try:
                            if esc.action == "ban":
                                await message.bot.ban_chat_member(
                                    chat_id=message.chat.id,
                                    user_id=target_id,
                                    until_date=esc.duration,
                                )
                            elif esc.action == "kick":
                                await message.bot.ban_chat_member(chat_id=message.chat.id, user_id=target_id)
                                await message.bot.unban_chat_member(chat_id=message.chat.id, user_id=target_id, only_if_banned=True)
                            elif esc.action == "mute":
                                no_perms = ChatPermissions(
                                    can_send_messages=False,
                                    can_send_media_messages=False,
                                    can_send_other_messages=False,
                                    can_add_web_page_previews=False,
                                )
                                await message.bot.restrict_chat_member(
                                    chat_id=message.chat.id,
                                    user_id=target_id,
                                    permissions=no_perms,
                                )
                        except Exception:
                            pass
            except Exception as esc_err:
                escalation_text = f"\n\n⚠️ Авто-наказание не применено: {esc_err}"

            await message.reply(
                f"⚠️ Пользователю ID <code>{target_id}</code> выдано предупреждение ({cnt}/{warn_limit or '∞'}). Причина: {reason}{escalation_text}",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().in_([
    "админы", "!админы", "/admins", "админ список", "!админ список", "состав", "!состав", "стаф", "!стаф", "администраторы", "!администраторы"
]))
async def handle_admin_list(message: types.Message):
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    async with session_scope() as session:
        admins = await get_chat_admins(session, chat_id=message.chat.id)
        if not admins:
            await message.reply("В чате еще не назначены администраторы Fable.")
            return
        lines = [f"🛡 <b>Администрация чата «{message.chat.title}»:</b>\n"]
        for p, lvl, role in admins:
            name = p.custom_nickname or p.first_name or f"ID {p.telegram_id}"
            uname_part = f" (@{p.username})" if p.username else ""
            lines.append(f"• <b>{name}</b>{uname_part} — Ранг {lvl} ({role})")
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().func(lambda t: any(t == prefix or t.startswith(f"{prefix} ") for prefix in [
    "наказания", "!наказания", "/punishments", "история наказаний", "проступки", "казы", "!казы"
])))
async def handle_punishment_history(message: types.Message):
    """История наказаний пользователя в чате (для модераторов, ур. 2+)."""
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    from app.services.moderation_service import get_user_moderation_history

    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=message.bot)
        if lvl < 2:
            return
        target_id = await _get_target_user_id(message, session)
        if not target_id and message.from_user:
            target_id = message.from_user.id
        if not target_id:
            await message.reply("Ответьте на сообщение или укажите ID/@username игрока.", parse_mode="HTML")
            return

        cases = await get_user_moderation_history(session, chat_id=message.chat.id, target_id=target_id)

    action_titles = {
        "warn": "⚠️ предупреждение",
        "mute": "🔇 мут",
        "ban": "⛔️ бан",
        "kick": "👢 кик",
    }
    if not cases:
        await message.reply(
            f"📜 У пользователя ID <code>{target_id}</code> нет истории наказаний в этом чате.",
            parse_mode="HTML",
        )
        return

    lines = [f"📜 <b>История наказаний</b> игрока ID <code>{target_id}</code>:\n"]
    for idx, c in enumerate(cases, start=1):
        title = action_titles.get(c.action, c.action)
        date_str = c.created_at.strftime("%d.%m.%Y %H:%M") if c.created_at else ""
        status = "активно" if c.is_active else "снято"
        lines.append(
            f"{idx}. {title} — <b>{c.reason or 'без причины'}</b>\n"
            f"   <i>{date_str}</i> • {status} • модератор ID {c.moderator_id}"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in ["чистка", "!чистка", "очистка", "!очистка"])))
async def handle_chat_purge(message: types.Message):
    """Очистка/удаление сообщений в чате для модераторов (ур. 2+).

    Форматы:
      • <code>чистка [N]</code> — удалить последние N сообщений (по умолчанию 10, до 200);
      • <code>чистка все [N]</code> — массовая очистка (по умолчанию 100, до 200);
      • ответом на сообщение пользователя + <code>чистка [N]</code> — удалить до N
        сообщений конкретного пользователя в пределах последних 300 сообщений.
    """
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    parts = (message.text or "").split()
    args = parts[1:]

    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=message.bot)
        if lvl < 2:
            await message.reply("Для чистки сообщений требуются права модератора (ур. 2+)")
            return
        target_id = await _get_target_user_id(message, session)

    # Разбираем «все» и число.
    purge_all = any(a.casefold() == "все" for a in args)
    count = 100 if purge_all else 10
    for a in args:
        if a.isdigit():
            count = int(a)
            break
    count = min(200, max(1, count))

    if not message.bot:
        return

    deleted = 0
    # Режим «по пользователю»: ответ на сообщение -> чистим только его сообщения.
    if target_id and message.reply_to_message:
        scan = min(300, max(count * 3, 60))
        cur_id = message.message_id
        removed_target = 0
        for mid in range(cur_id, max(1, cur_id - scan - 1), -1):
            if removed_target >= count:
                break
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=mid)
                removed_target += 1
            except Exception:
                continue
        deleted = removed_target
    else:
        try:
            await message.delete()
        except Exception:
            pass
        cur_id = message.message_id
        for mid in range(cur_id - 1, max(1, cur_id - count - 1), -1):
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=mid)
                deleted += 1
            except Exception:
                continue

    try:
        notify = await message.answer(f"🧹 Удалено сообщений: <b>{deleted}</b>", parse_mode="HTML")
        import asyncio
        await asyncio.sleep(3)
        await notify.delete()
    except Exception:
        pass


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in ["закрепить", "!закрепить", "пин", "!пин", "pin"])))
async def handle_pin_message(message: types.Message):
    """Закрепление сообщения в группе для админов (ур. 3+)."""
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    if not message.reply_to_message:
        await message.reply("Ответьте на сообщение, которое нужно закрепить: <code>закрепить</code>", parse_mode="HTML")
        return

    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=message.bot)
        if lvl < 3:
            await message.reply("Закреплять сообщения могут администраторы (ур. 3+)")
            return

    if message.bot:
        try:
            await message.bot.pin_chat_message(chat_id=message.chat.id, message_id=message.reply_to_message.message_id)
            await message.reply("📌 Сообщение успешно закреплено!")
        except Exception as e:
            await message.reply(f"❌ Не удалось закрепить сообщение: {e}")


@router.message(F.text.casefold().func(lambda t: any(t.startswith(prefix) for prefix in ["открепить", "!открепить", "анпин", "!анпин", "unpin"])))
async def handle_unpin_message(message: types.Message):
    """Открепление сообщения в группе для админов (ур. 3+)."""
    if not message.chat or message.chat.type in (ChatType.PRIVATE, "private"):
        return
    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=message.chat.id, user_id=message.from_user.id, bot=message.bot)
        if lvl < 3:
            await message.reply("Откреплять сообщения могут администраторы (ур. 3+)")
            return

    if message.bot:
        try:
            target_msg_id = message.reply_to_message.message_id if message.reply_to_message else None
            await message.bot.unpin_chat_message(chat_id=message.chat.id, message_id=target_msg_id)
            await message.reply("🔓 Сообщение откреплено!")
        except Exception as e:
            await message.reply(f"❌ Не удалось открепить сообщение: {e}")
