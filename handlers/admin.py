"""Хэндлеры администратора: пошаговое создание конкурса (aiogram 3, FSM).

Мастер построен на :class:`StatesGroup`: на каждом шаге ввод сохраняется в
``FSMContext`` через ``state.update_data(...)``. В конце конкурс сохраняется в
JSON-базу (``data.create_contest``) со статусом «активный» и публикуется пост с
кнопкой участия из ``Keyboards.конкурс_клавиатура``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from aiogram import Bot, F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from data import create_contest
from Keyboards import конкурс_клавиатура

logger = logging.getLogger(__name__)

router = Router(name="admin")

STATUS_ACTIVE = "активный"
DATE_FORMAT = "%d.%m.%Y %H:%M"
DATE_HINT = "Формат даты: ДД.ММ.ГГГГ ЧЧ:ММ (например, 31.12.2026 23:59)."

BTN_CANCEL = "✖️ Отмена"


class CreateContest(StatesGroup):
    """Состояния мастера создания конкурса."""

    title = State()
    prize = State()
    channel = State()
    ends_at = State()
    winners_count = State()


def _is_admin(user_id: int) -> bool:
    """Администратор — владелец (OWNER_ID) либо id из ADMIN_IDS."""
    raw = os.getenv("ADMIN_IDS", "")
    ids = {int(chunk) for chunk in raw.replace(";", ",").split(",") if chunk.strip().isdigit()}
    if user_id in ids:
        return True
    owner = os.getenv("OWNER_ID", "")
    return owner.isdigit() and user_id == int(owner)


def _normalize_channel(text: str) -> str:
    text = text.strip()
    if not text.startswith("@") and not text.startswith("-"):
        text = "@" + text
    return text


async def _bot_is_channel_admin(bot: Bot, channel: str) -> bool:
    """Проверить через ``get_chat_member``, что бот — админ канала."""
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=bot.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_chat_member(%s): %s", channel, exc)
        return False
    return member.status in ("administrator", "creator")


def _parse_end_date(text: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), DATE_FORMAT).isoformat()
    except ValueError:
        return None


# --- Запуск мастера -------------------------------------------------------

@router.message(Command("create"))
@router.message(F.text == "👑 Создать конкурс")
async def start_create(message: types.Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("Недостаточно прав.")
        return
    await state.set_state(CreateContest.title)
    await state.update_data(title=None, prize=None, channel=None, ends_at=None, winners_count=None)
    await message.answer(
        "Создание конкурса. Введи название конкурса:\n\n"
        f"Отменить в любой момент: {BTN_CANCEL}"
    )


@router.message(F.text == BTN_CANCEL)
async def cancel_create(message: types.Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Создание конкурса отменено.")


# --- Шаг 1: название ------------------------------------------------------

@router.message(CreateContest.title)
async def step_title(message: types.Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым. Введи название конкурса:")
        return
    await state.update_data(title=title)
    await state.set_state(CreateContest.prize)
    await message.answer("Введи приз (что разыгрывается):")


# --- Шаг 2: приз ----------------------------------------------------------

@router.message(CreateContest.prize)
async def step_prize(message: types.Message, state: FSMContext) -> None:
    prize = (message.text or "").strip()
    if not prize:
        await message.answer("Приз не может быть пустым. Введи приз:")
        return
    await state.update_data(prize=prize)
    await state.set_state(CreateContest.channel)
    await message.answer("Укажи @username канала конкурса (бот должен быть там админом):")


# --- Шаг 3: канал + проверка прав бота ------------------------------------

@router.message(CreateContest.channel)
async def step_channel(message: types.Message, state: FSMContext, bot: Bot) -> None:
    channel = _normalize_channel(message.text or "")
    if len(channel) < 2:
        await message.answer("Укажи корректный @username канала:")
        return
    if not await _bot_is_channel_admin(bot, channel):
        await message.answer(
            "Бот не админ в этом канале (или канал не найден). "
            "Добавь бота администратором и пришли @username снова:"
        )
        return
    await state.update_data(channel=channel)
    await state.set_state(CreateContest.ends_at)
    await message.answer("Когда завершить конкурс? " + DATE_HINT)


# --- Шаг 4: дата окончания ------------------------------------------------

@router.message(CreateContest.ends_at)
async def step_ends_at(message: types.Message, state: FSMContext) -> None:
    parsed = _parse_end_date(message.text or "")
    if parsed is None:
        await message.answer("Не понял дату. " + DATE_HINT)
        return
    await state.update_data(ends_at=parsed)
    await state.set_state(CreateContest.winners_count)
    await message.answer("Сколько победителей?")


# --- Шаг 5: число победителей + сохранение и публикация -------------------

@router.message(CreateContest.winners_count)
async def step_winners_count(message: types.Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("Введи целое число победителей (1 или больше):")
        return

    data = await state.get_data()
    await state.clear()

    contest = {
        "title": data.get("title"),
        "prize": data.get("prize"),
        "channel": data.get("channel"),
        "ends_at": data.get("ends_at"),
        "winners_count": int(text),
        "status": STATUS_ACTIVE,
        "participants": [],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    saved = await create_contest(contest)

    published = await _publish_contest(bot, saved)
    suffix = "" if published else "\n⚠️ Не удалось опубликовать пост в канал — проверь права бота."
    await message.answer(
        f"✅ Конкурс «{saved['title']}» создан (id {saved['id']}).{suffix}"
    )


async def _publish_contest(bot: Bot, contest: dict) -> bool:
    text = (
        f"🎉 Розыгрыш: {contest['title']}\n"
        f"🎁 Приз: {contest.get('prize', '')}\n"
        f"⏰ Итоги: {contest.get('ends_at', '')}\n\n"
        "Нажми «Участвовать», чтобы принять участие!"
    )
    try:
        await bot.send_message(
            chat_id=contest["channel"],
            text=text,
            reply_markup=конкурс_клавиатура(contest["id"]),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось опубликовать конкурс %s: %s", contest.get("id"), exc)
        return False
