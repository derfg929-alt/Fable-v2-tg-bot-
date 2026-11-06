"""Обработчики глобальной панели владельца Fable (Telegram ID 8039111975):
- "настройки чаты"
- "настройки стоп ID" / "настройки старт ID"
- "глобальный бан @user [причина]"
- "глобальный разбан @user"
- "сброс экономики" (с кнопками подтверждения)
"""
from __future__ import annotations

from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.db.session import session_scope
from app.services.owner_service import (
    verify_owner,
    list_chats_summary,
    list_players_wealth_summary,
    get_player_wealth_details,
    set_chat_bot_state,
    set_player_global_ban,
    reset_global_economy,
    OwnerAccessDeniedError,
)

router = Router(name="owner_router")


def _is_owner(user_id: int) -> bool:
    return user_id == 8039111975 or user_id == get_settings().owner_id


async def _build_admin_panel_payload(owner_id: int):
    """Формирует текст и клавиатуру админ-панели с подробным списком чатов."""
    async with session_scope() as session:
        chats = await list_chats_summary(session, owner_id=owner_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="🏢 Список чатов", callback_data="owner_menu_chats")
    builder.button(text="💰 Балансы игроков", callback_data="owner_menu_balances")
    builder.button(text="📊 Статистика", callback_data="owner_menu_stats")
    builder.button(text="⚠️ Сброс экономики", callback_data="owner_eco_reset_prompt")
    builder.adjust(2, 2)

    active_cnt = sum(1 for c in chats if c.is_active)
    total_cnt = len(chats)

    if chats:
        chat_lines = []
        for c in chats[:15]:
            status_icon = "🟢" if c.is_active else "🔴"
            chat_type_str = f" [{c.chat_type}]" if c.chat_type else ""
            chat_lines.append(f"• {status_icon} <b>{c.title or 'Без названия'}</b>{chat_type_str} (ID: <code>{c.telegram_id}</code>)")
        if len(chats) > 15:
            chat_lines.append(f"<i>...и еще {len(chats) - 15} чатов (нажмите «🏢 Список чатов» для просмотра всех)</i>")
        chats_text = "\n".join(chat_lines)
    else:
        chats_text = "<i>Бот пока не подключен ни к одному чату.</i>"

    text = (
        f"👑 <b>ГЛАВНАЯ АДМИН-ПАНЕЛЬ ВЛАДЕЛЬЦА FABLE</b>\n\n"
        f"Добро пожаловать, Создатель! (ID: <code>{owner_id}</code>)\n\n"
        f"🏢 <b>Подключенные чаты ({active_cnt}/{total_cnt} активны):</b>\n"
        f"{chats_text}\n\n"
        f"<b>Доступные глобальные команды:</b>\n"
        f"• <code>балансы</code> / <code>кто сколько</code> — балансы всех игроков\n"
        f"• <code>инфо [ID]</code> — баланс и состояние конкретного игрока\n"
        f"• <code>настройки чаты</code> — полный список подключенных групп\n"
        f"• <code>настройки стоп [ID]</code> — отключить бота в группе\n"
        f"• <code>настройки старт [ID]</code> — включить бота в группе\n"
        f"• <code>выдать [ID] [монеты]</code> — начислить монеты игроку\n"
        f"• <code>забрать [ID] [монеты]</code> — списать монеты у игрока\n"
        f"• <code>глобальный бан [ID] [причина]</code>\n"
        f"• <code>глобальный разбан [ID]</code>\n"
        f"• <code>сброс экономики</code>"
    )
    return text, builder.as_markup()


@router.message(F.text.casefold().in_(["понель", "!понель", "панель", "!панель", "админка"]))
async def handle_admin_panel(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    text, markup = await _build_admin_panel_payload(message.from_user.id)
    await message.reply(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "owner_menu_back")
async def on_owner_menu_back(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещен!", show_alert=True)
        return
    try:
        await callback.answer()
    except Exception:
        pass
    text, markup = await _build_admin_panel_payload(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data == "owner_menu_chats")
async def on_owner_menu_chats(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещен!", show_alert=True)
        return
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        chats = await list_chats_summary(session, owner_id=callback.from_user.id)
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад в админ-панель", callback_data="owner_menu_back")

        if not chats:
            try:
                await callback.message.edit_text("🏢 Бот пока не подключен ни к одному чату.", reply_markup=builder.as_markup())
            except Exception:
                pass
            return

        active_cnt = sum(1 for c in chats if c.is_active)
        lines = [f"🏢 <b>Список чатов, где состоит Fable ({active_cnt}/{len(chats)} активны):</b>\n"]
        for c in chats[:30]:
            status = "🟢 Активен" if c.is_active else "🔴 Остановлен"
            connected = c.connected_at.strftime("%d.%m.%Y") if c.connected_at else ""
            conn_str = f" | {connected}" if connected else ""
            lines.append(f"• <b>{c.title or 'Без названия'}</b>\n  ID: <code>{c.telegram_id}</code> | Тип: {c.chat_type} | {status}{conn_str}")
        if len(chats) > 30:
            lines.append(f"\n<i>...и еще {len(chats) - 30} чатов</i>")

        try:
            await callback.message.edit_text("\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(F.data == "owner_menu_balances")
async def on_owner_menu_balances(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещен!", show_alert=True)
        return
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        players = await list_players_wealth_summary(session, owner_id=callback.from_user.id, limit=20)
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад в админ-панель", callback_data="owner_menu_back")

        if not players:
            try:
                await callback.message.edit_text("💰 В базе пока нет зарегистрированных игроков.", reply_markup=builder.as_markup())
            except Exception:
                pass
            return

        lines = ["💰 <b>Балансы игроков Fable (топ по богатству):</b>\n"]
        for p in players:
            name = p.first_name or f"ID {p.telegram_id}"
            uname = f" (@{p.username})" if p.username else ""
            total = p.wallet + p.bank_balance
            lines.append(
                f"• <b>{name}</b>{uname} [<code>{p.telegram_id}</code>]\n"
                f"   💵 Кошелек: {p.wallet:,} | 🏦 Банк: {p.bank_balance:,} | Всего: <b>{total:,}</b>"
            )

        text = "\n".join(lines).replace(",", " ")
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            pass


@router.message(F.text.casefold().in_(["балансы", "!балансы", "кто сколько", "!кто сколько"]))
async def handle_owner_players_balances(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    async with session_scope() as session:
        players = await list_players_wealth_summary(session, owner_id=message.from_user.id, limit=25)
        if not players:
            await message.reply("В базе пока нет игроков.")
            return

        lines = ["💰 <b>Балансы игроков Fable (топ по богатству):</b>\n"]
        for p in players:
            name = p.first_name or f"ID {p.telegram_id}"
            uname = f" (@{p.username})" if p.username else ""
            total = p.wallet + p.bank_balance
            lines.append(
                f"• <b>{name}</b>{uname} (ID: <code>{p.telegram_id}</code>)\n"
                f"   💵 На руках: {p.wallet:,} | 🏦 В банке: {p.bank_balance:,} | Всего: <b>{total:,}</b>"
            )
        await message.reply("\n".join(lines).replace(",", " "), parse_mode="HTML")


@router.message(F.text.casefold().startswith("инфо"))
async def handle_owner_player_info(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(parts) >= 2 and parts[1].isdigit():
        target_id = int(parts[1])

    if not target_id:
        await message.reply("Использование: <code>инфо [ID]</code> или ответом на сообщение игрока", parse_mode="HTML")
        return

    async with session_scope() as session:
        p = await get_player_wealth_details(session, owner_id=message.from_user.id, target_id=target_id)
        if not p:
            await message.reply(f"❌ Игрок с ID <code>{target_id}</code> не найден в базе данных.", parse_mode="HTML")
            return

        total_wealth = p.wallet + p.bank_balance
        ban_status = f"🔴 Забанен ({p.global_ban_reason})" if p.is_globally_banned else "🟢 Активен"
        reg_date = p.created_at.strftime("%d.%m.%Y %H:%M") if p.created_at else "Неизвестно"

        text = (
            f"👤 <b>ИНФОРМАЦИЯ ОБ ИГРОКЕ</b>\n\n"
            f"ID: <code>{p.telegram_id}</code>\n"
            f"Имя: <b>{p.first_name}</b>\n"
            f"Юзернейм: <b>@{p.username or 'отсутствует'}</b>\n"
            f"Статус: {ban_status}\n"
            f"Регистрация: <i>{reg_date}</i>\n\n"
            f"💵 В кошельке: <b>{p.wallet:,}</b> монет\n"
            f"🏦 В банке: <b>{p.bank_balance:,}</b> монет\n"
            f"💰 Общее состояние: <b>{total_wealth:,}</b> монет\n"
            f"📈 Акций: <b>{p.shares:,}</b> шт.\n"
            f"⭐ Опыт: <b>{p.experience:,}</b> XP"
        ).replace(",", " ")
        await message.reply(text, parse_mode="HTML")


@router.callback_query(F.data == "owner_menu_stats")
async def on_owner_menu_stats(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещен!", show_alert=True)
        return
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        from app.db.models import Player, Chat, EconomyTransaction
        from sqlalchemy import func, select
        players_cnt = await session.scalar(select(func.count(Player.telegram_id))) or 0
        chats_cnt = await session.scalar(select(func.count(Chat.telegram_id))) or 0
        active_chats_cnt = await session.scalar(select(func.count(Chat.telegram_id)).where(Chat.is_active == True)) or 0
        total_coins = await session.scalar(select(func.coalesce(func.sum(Player.wallet), 0))) or 0
        total_bank = await session.scalar(select(func.coalesce(func.sum(Player.bank_balance), 0))) or 0
        tx_cnt = await session.scalar(select(func.count(EconomyTransaction.id))) or 0

        chats = await list_chats_summary(session, owner_id=callback.from_user.id)
        builder = InlineKeyboardBuilder()
        builder.button(text="🏢 Список чатов", callback_data="owner_menu_chats")
        builder.button(text="⬅️ Назад в админ-панель", callback_data="owner_menu_back")
        builder.adjust(1)

        chat_sample_lines = []
        for c in chats[:10]:
            status = "🟢" if c.is_active else "🔴"
            chat_sample_lines.append(f"• {status} <b>{c.title or 'Без названия'}</b> (<code>{c.telegram_id}</code>)")
        if len(chats) > 10:
            chat_sample_lines.append(f"<i>...и еще {len(chats) - 10} чатов</i>")
        chats_block = "\n".join(chat_sample_lines) if chat_sample_lines else "<i>Нет подключенных чатов</i>"

        text = (
            f"📊 <b>ГЛОБАЛЬНАЯ СТАТИСТИКА FABLE</b>\n\n"
            f"👥 Всего зарегистрировано игроков: <b>{players_cnt:,}</b>\n"
            f"💬 Всего подключенных групп: <b>{chats_cnt:,}</b> (активных: <b>{active_chats_cnt}</b>)\n"
            f"💰 Монет на руках у игроков: <b>{total_coins:,}</b>\n"
            f"🏦 Монет на банковских счетах: <b>{total_bank:,}</b>\n"
            f"🏛 Всего в экономике: <b>{(total_coins + total_bank):,}</b> монет\n"
            f"📜 Проведено финансовых транзакций: <b>{tx_cnt:,}</b>\n\n"
            f"🏢 <b>Чаты с ботом:</b>\n"
            f"{chats_block}"
        ).replace(",", " ")
        try:
            await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            pass


@router.message(F.text.casefold().startswith("выдать"))
async def handle_give_coins(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.reply("Использование: <code>выдать [ID] [сумма]</code>", parse_mode="HTML")
        return
    target_id = int(parts[1])
    amount = int(parts[2])
    async with session_scope() as session:
        from app.services.economy_service import credit_coins
        from app.services.player_service import get_or_create_player
        await get_or_create_player(session, telegram_id=target_id)
        try:
            res = await credit_coins(
                session,
                user_id=target_id,
                amount=amount,
                reason="Выдача администратором",
                operation_type="admin_grant",
                actor_id=message.from_user.id,
            )
            await message.reply(
                f"✅ Выдано <b>{amount:,}</b> монет игроку <code>{target_id}</code>!\n"
                f"Его новый баланс: <b>{res.current_balance:,}</b> монет.".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("забрать"))
async def handle_take_coins(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.reply("Использование: <code>забрать [ID] [сумма]</code>", parse_mode="HTML")
        return
    target_id = int(parts[1])
    amount = int(parts[2])
    async with session_scope() as session:
        from app.services.economy_service import debit_coins
        try:
            res = await debit_coins(
                session,
                user_id=target_id,
                amount=amount,
                reason="Списание администратором",
                operation_type="admin_revoke",
                actor_id=message.from_user.id,
            )
            await message.reply(
                f"✅ Списано <b>{amount:,}</b> монет у игрока <code>{target_id}</code>!\n"
                f"Его остаток: <b>{res.current_balance:,}</b> монет.".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("настройки чаты"))
async def handle_owner_chats(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    async with session_scope() as session:
        chats = await list_chats_summary(session, owner_id=message.from_user.id)
        if not chats:
            await message.reply("Бот пока не подключен ни к одному чату.")
            return

        active_cnt = sum(1 for c in chats if c.is_active)
        lines = [f"🏢 <b>Подключенные чаты Fable ({active_cnt}/{len(chats)} активны):</b>\n"]
        for c in chats:
            status = "🟢 Активен" if c.is_active else "🔴 Остановлен"
            lines.append(f"• <b>{c.title or 'Без названия'}</b>\n  ID: <code>{c.telegram_id}</code> | Тип: {c.chat_type} — {status}")
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("настройки стоп"))
async def handle_owner_stop_chat(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        return
    try:
        target_chat_id = int(parts[2])
        async with session_scope() as session:
            chat = await set_chat_bot_state(session, owner_id=message.from_user.id, chat_id=target_chat_id, is_active=False)
            await message.reply(f"🔴 Бот остановлен в чате <b>{chat.title}</b> (<code>{chat.telegram_id}</code>)", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("настройки старт"))
async def handle_owner_start_chat(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        return
    try:
        target_chat_id = int(parts[2])
        async with session_scope() as session:
            chat = await set_chat_bot_state(session, owner_id=message.from_user.id, chat_id=target_chat_id, is_active=True)
            await message.reply(f"🟢 Бот возобновлен в чате <b>{chat.title}</b> (<code>{chat.telegram_id}</code>)", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("глобальный бан"))
async def handle_global_ban(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(parts) >= 3 and parts[2].isdigit():
        target_id = int(parts[2])

    if not target_id:
        await message.reply("Использование: ответом или <code>глобальный бан ID [причина]</code>", parse_mode="HTML")
        return

    reason = " ".join(parts[3:]) if len(parts) > 3 else "Блокировка владельцем"
    async with session_scope() as session:
        try:
            player = await set_player_global_ban(session, owner_id=message.from_user.id, target_id=target_id, banned=True, reason=reason)
            await message.reply(f"🚫 Игроку ID <code>{target_id}</code> выдан глобальный бан Fable.\nПричина: {reason}", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("глобальный разбан"))
async def handle_global_unban(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    target_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    elif len(parts) >= 3 and parts[2].isdigit():
        target_id = int(parts[2])

    if not target_id:
        await message.reply("Использование: ответом или <code>глобальный разбан ID</code>", parse_mode="HTML")
        return

    async with session_scope() as session:
        try:
            player = await set_player_global_ban(session, owner_id=message.from_user.id, target_id=target_id, banned=False)
            await message.reply(f"✅ Глобальный бан с игрока ID <code>{target_id}</code> снят.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


def _build_economy_reset_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⚠️ ДА, СБРОСИТЬ", callback_data="owner_eco_reset_confirm")
    builder.button(text="❌ Отмена", callback_data="owner_eco_reset_cancel")
    builder.adjust(2)
    return builder.as_markup()


@router.message(F.text.casefold().in_(["сброс экономики", "!сброс экономики"]))
async def handle_reset_economy_prompt(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    await message.reply(
        "🚨 <b>ВНИМАНИЕ! ОПАСНАЯ ОПЕРАЦИЯ!</b>\n\n"
        "Вы действительно хотите сбросить глобальную экономику?\n"
        "Все балансы игроков, банковские счета и акции будут обнулены!\n"
        "История операций и аккаунты сохранятся.",
        reply_markup=_build_economy_reset_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "owner_eco_reset_prompt")
async def on_owner_eco_reset_prompt(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещен!", show_alert=True)
        return
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text(
            "🚨 <b>ВНИМАНИЕ! ОПАСНАЯ ОПЕРАЦИЯ!</b>\n\n"
            "Вы действительно хотите сбросить глобальную экономику?\n"
            "Все балансы игроков, банковские счета и акции будут обнулены!\n"
            "История операций и аккаунты сохранятся.",
            reply_markup=_build_economy_reset_keyboard(),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data == "owner_eco_reset_confirm")
async def on_reset_economy_confirm(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Только владелец!", show_alert=True)
        return
    try:
        await callback.answer("Сброс выполняется...")
    except Exception:
        pass
    async with session_scope() as session:
        count = await reset_global_economy(session, owner_id=callback.from_user.id)
        try:
            await callback.message.edit_text(f"💥 <b>Глобальный сброс выполнен!</b>\nОбнулены балансы {count} игроков.", parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(F.data == "owner_eco_reset_cancel")
async def on_reset_economy_cancel(callback: types.CallbackQuery):
    try:
        await callback.answer("Сброс отменен.")
    except Exception:
        pass
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в админ-панель", callback_data="owner_menu_back")
    try:
        await callback.message.edit_text("Сброс экономики отменен.", reply_markup=builder.as_markup())
    except Exception:
        pass
