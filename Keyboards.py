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

BTN_ACTIVE = "🎁 Активные розыгрыши"
BTN_MY = "🎟 Мои билеты"
BTN_CREATE = "👑 Создать конкурс"


def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Reply-клавиатура главного меню.

    Создавать конкурсы может любой пользователь, поэтому кнопка создания видна
    всем. Публикация в канал требует прав владельца/администратора канала и
    админки бота там же; без канала конкурс открыт для всех.
    """
    keyboard = [
        [KeyboardButton(text=BTN_ACTIVE)],
        [KeyboardButton(text=BTN_MY)],
        [KeyboardButton(text=BTN_CREATE)],
    ]
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
