"""Хэндлеры администратора: запуск и ведение мастера создания конкурса."""
from __future__ import annotations

from aiogram import F, Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery

from .. import admin_flow, config, database, keyboards

router = Router(name="admin")

ADMIN_CALLBACKS = {
    "add_sponsor",
    "sponsors_done",
    "set_ref_tickets",
    "toggle_codes",
    "toggle_tasks",
    "toggle_show_participants",
    "toggle_show_tickets",
    "toggle_share",
    "toggle_leaderboard",
    "set_reserve",
    "finish_contest",
    "cancel",
}


@router.message(F.text == keyboards.BTN_CREATE)
async def on_create(message: types.Message) -> None:
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        await message.answer("Недостаточно прав.")
        return
    reply = await admin_flow.start_creation(user_id)
    await message.answer(reply.text, reply_markup=reply.keyboard)


@router.callback_query(F.data.in_(ADMIN_CALLBACKS))
async def on_admin_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if not config.is_admin(user_id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    reply = await admin_flow.handle_callback(user_id, callback.data, callback.bot)
    if reply is None:
        return
    await callback.message.answer(reply.text, reply_markup=reply.keyboard)


@router.message(F.text.regexp(r"^[^\s/].*"))
async def on_admin_text(message: types.Message) -> None:
    user_id = message.from_user.id
    if not config.is_admin(user_id):
        raise SkipHandler()
    state = await database.get_admin_state(user_id)
    if not state.get("step"):
        # Админ без активного мастера — текст может быть кодовым словом.
        raise SkipHandler()
    reply = await admin_flow.handle_text(user_id, message.text or "", message.bot)
    if reply is None:
        raise SkipHandler()
    await message.answer(reply.text, reply_markup=reply.keyboard)
