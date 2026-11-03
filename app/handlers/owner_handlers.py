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
    return user_id == get_settings().owner_id


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


@router.message(F.text.casefold().in_(["сброс экономики", "!сброс экономики"]))
async def handle_reset_economy_prompt(message: types.Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="⚠️ ДА, СБРОСИТЬ", callback_data="owner_eco_reset_confirm")
    builder.button(text="❌ Отмена", callback_data="owner_eco_reset_cancel")
    builder.adjust(2)
    await message.reply(
        "🚨 <b>ВНИМАНИЕ! ОПАСНАЯ ОПЕРАЦИЯ!</b>\n\n"
        "Вы действительно хотите сбросить глобальную экономику?\n"
        "Все балансы игроков, банковские счета и акции будут обнулены!\n"
        "История операций и аккаунты сохранятся.",
        reply_markup=builder.as_markup(),
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
