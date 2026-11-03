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
    set_chat_bot_state,
    set_player_global_ban,
    reset_global_economy,
    OwnerAccessDeniedError,
)

router = Router(name="owner_router")


def _is_owner(user_id: int) -> bool:
    return user_id == 8039111975 or user_id == get_settings().owner_id


@router.message(F.text.casefold().in_(["понель", "!понель", "панель", "!панель", "админка"]))
async def handle_admin_panel(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="🏢 Чаты", callback_data="owner_menu_chats")
    builder.button(text="📊 Статистика", callback_data="owner_menu_stats")
    builder.button(text="⚠️ Сброс экономики", callback_data="owner_eco_reset_prompt")
    builder.adjust(2, 1)

    text = (
        f"👑 <b>ГЛАВНАЯ АДМИН-ПАНЕЛЬ ВЛАДЕЛЬЦА FABLE</b>\n\n"
        f"Добро пожаловать, Создатель! (ID: <code>{message.from_user.id}</code>)\n\n"
        f"<b>Доступные глобальные команды:</b>\n"
        f"• <code>настройки чаты</code> — список подключенных групп\n"
        f"• <code>настройки стоп [ID]</code> — отключить бота в группе\n"
        f"• <code>настройки старт [ID]</code> — включить бота в группе\n"
        f"• <code>выдать [ID] [монеты]</code> — начислить монеты игроку\n"
        f"• <code>забрать [ID] [монеты]</code> — списать монеты у игрока\n"
        f"• <code>глобальный бан [ID] [причина]</code>\n"
        f"• <code>глобальный разбан [ID]</code>\n"
        f"• <code>сброс экономики</code>"
    )
    await message.reply(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "owner_menu_chats")
async def on_owner_menu_chats(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещен!", show_alert=True)
        return
    async with session_scope() as session:
        chats = await list_chats_summary(session, owner_id=callback.from_user.id)
        if not chats:
            await callback.message.edit_text("Бот пока не подключен ни к одному чату.")
            return

        lines = ["🏢 <b>Подключенные чаты Fable:</b>\n"]
        for c in chats[:20]:
            status = "🟢 Активен" if c.is_active else "🔴 Остановлен"
            lines.append(f"• <code>{c.telegram_id}</code>: <b>{c.title}</b> — {status}")
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "owner_menu_stats")
async def on_owner_menu_stats(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Доступ запрещен!", show_alert=True)
        return
    async with session_scope() as session:
        from app.db.models import Player, Chat, EconomyTransaction
        from sqlalchemy import func, select
        players_cnt = await session.scalar(select(func.count(Player.telegram_id))) or 0
        chats_cnt = await session.scalar(select(func.count(Chat.telegram_id))) or 0
        total_coins = await session.scalar(select(func.coalesce(func.sum(Player.wallet), 0))) or 0
        total_bank = await session.scalar(select(func.coalesce(func.sum(Player.bank_balance), 0))) or 0
        tx_cnt = await session.scalar(select(func.count(EconomyTransaction.id))) or 0

        text = (
            f"📊 <b>ГЛОБАЛЬНАЯ СТАТИСТИКА FABLE</b>\n\n"
            f"👥 Всего зарегистрировано игроков: <b>{players_cnt:,}</b>\n"
            f"💬 Всего подключенных групп: <b>{chats_cnt:,}</b>\n"
            f"💰 Монет на руках у игроков: <b>{total_coins:,}</b>\n"
            f"🏦 Монет на банковских счетах: <b>{total_bank:,}</b>\n"
            f"🏛 Всего в экономике: <b>{(total_coins + total_bank):,}</b> монет\n"
            f"📜 Проведено финансовых транзакций: <b>{tx_cnt:,}</b>"
        ).replace(",", " ")
        await callback.message.edit_text(text, parse_mode="HTML")


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

        lines = ["🏢 <b>Подключенные чаты Fable:</b>\n"]
        for c in chats:
            status = "🟢 Активен" if c.is_active else "🔴 Остановлен"
            lines.append(f"• <code>{c.telegram_id}</code>: <b>{c.title}</b> — {status}")
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
    await callback.message.edit_text(
        "🚨 <b>ВНИМАНИЕ! ОПАСНАЯ ОПЕРАЦИЯ!</b>\n\n"
        "Вы действительно хотите сбросить глобальную экономику?\n"
        "Все балансы игроков, банковские счета и акции будут обнулены!\n"
        "История операций и аккаунты сохранятся.",
        reply_markup=_build_economy_reset_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "owner_eco_reset_confirm")
async def on_reset_economy_confirm(callback: types.CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Только владелец!", show_alert=True)
        return
    async with session_scope() as session:
        count = await reset_global_economy(session, owner_id=callback.from_user.id)
        await callback.message.edit_text(f"💥 <b>Глобальный сброс выполнен!</b>\nОбнулены балансы {count} игроков.", parse_mode="HTML")


@router.callback_query(F.data == "owner_eco_reset_cancel")
async def on_reset_economy_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("Сброс экономики отменен.")
