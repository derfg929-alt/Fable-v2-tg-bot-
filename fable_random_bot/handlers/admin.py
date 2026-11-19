"""Хэндлеры администратора: запуск и ведение мастера создания конкурса."""
from __future__ import annotations

from aiogram import F, Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery

from .. import admin_flow, database, keyboards

router = Router(name="admin")

ADMIN_CALLBACKS = {
    "add_sponsor",
    "skip_channel",
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
    reply = await admin_flow.start_creation(user_id)
    await message.answer(reply.text, reply_markup=reply.keyboard)


@router.callback_query(F.data.in_(ADMIN_CALLBACKS))
async def on_admin_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    await callback.answer()
    reply = await admin_flow.handle_callback(user_id, callback.data, callback.bot)
    if reply is None:
        return
    await callback.message.answer(reply.text, reply_markup=reply.keyboard)


@router.message(F.text.regexp(r"^[^\s/].*"))
async def on_admin_text(message: types.Message) -> None:
    """Текст шага мастера создания конкурса.

    Создавать конкурсы может любой пользователь, поэтому гейта по ADMIN_IDS нет.
    Текст потребляется только если у пользователя активен шаг мастера; иначе
    событие пропускается дальше (например, кодовые слова в user-роутере).
    """
    user_id = message.from_user.id
    state = await database.get_admin_state(user_id)
    if not state.get("step"):
        raise SkipHandler()
    reply = await admin_flow.handle_text(user_id, message.text or "", message.bot)
    if reply is None:
        raise SkipHandler()
    await message.answer(reply.text, reply_markup=reply.keyboard)
