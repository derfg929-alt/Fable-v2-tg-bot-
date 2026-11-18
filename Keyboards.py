"""Клавиатуры бота розыгрышей для aiogram 3.

Reply-клавиатура главного меню и inline-клавиатура участия в конкурсе.
"""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from fable_random_bot import config

BTN_ACTIVE = "🎁 Активные розыгрыши"
BTN_MY = "🎟 Мои билеты"
BTN_CREATE = "👑 Создать конкурс"


def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Reply-клавиатура главного меню.

    Кнопка «Создать конкурс» показывается только администраторам (ADMIN_IDS).
    """
    keyboard = [
        [KeyboardButton(text=BTN_ACTIVE)],
        [KeyboardButton(text=BTN_MY)],
    ]
    if user_id in config.ADMIN_IDS:
        keyboard.append([KeyboardButton(text=BTN_CREATE)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def конкурс_клавиатура(contest_id: int | str) -> InlineKeyboardMarkup:
    """Inline-клавиатура конкурса с кнопкой участия."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Участвовать",
                    callback_data=f"join_{contest_id}",
                )
            ]
        ]
    )
