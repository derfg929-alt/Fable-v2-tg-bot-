"""Клавиатуры бота: Reply (главное меню) и Inline (участие, настройки)."""
from __future__ import annotations

from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_ACTIVE = "🎁 Активные розыгрыши"
BTN_MY = "🎟 Мои билеты и рефералы"
BTN_CREATE = "👑 Создать конкурс"


def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Reply-клавиатура главного меню.

    Создавать конкурсы может любой пользователь, поэтому кнопка создания видна
    всегда. Для публикации конкурса в канале пользователь должен быть
    владельцем/администратором канала, а бот — администратором канала.
    """
    rows = [
        [KeyboardButton(text=BTN_ACTIVE)],
        [KeyboardButton(text=BTN_MY)],
        [KeyboardButton(text=BTN_CREATE)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def join_keyboard(contest_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Участвовать", callback_data=f"join_{contest_id}")]
        ]
    )


def join_failed_keyboard(contest_id: str, channels: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for channel in channels:
        if channel.startswith("@"):
            rows.append(
                [InlineKeyboardButton(text=f"Подписаться {channel}", url=f"https://t.me/{channel[1:]}")]
            )
    rows.append(
        [InlineKeyboardButton(text="Проверить снова", callback_data=f"check_{contest_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def share_keyboard(contest_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Поделиться", callback_data=f"share_{contest_id}")]
        ]
    )


def sponsors_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить спонсора", callback_data="add_sponsor")],
            [InlineKeyboardButton(text="✅ Готово", callback_data="sponsors_done")],
        ]
    )


def _on_off(value: Any) -> str:
    return "Вкл" if value else "Выкл"


def settings_keyboard(temp: dict[str, Any]) -> InlineKeyboardMarkup:
    """Inline-клавиатура настроек конкурса. Текст кнопок отражает состояние."""
    settings = temp.setdefault("settings", {})
    rows = [
        [
            InlineKeyboardButton(
                text=f"Билетов за реферала: {settings.get('tickets_per_ref', 5)}",
                callback_data="set_ref_tickets",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Кодовые слова: {_on_off(settings.get('code_words'))}",
                callback_data="toggle_codes",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Задания спонсоров: {_on_off(settings.get('sponsor_tasks'))}",
                callback_data="toggle_tasks",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Показывать участников: {_on_off(settings.get('show_participants'))}",
                callback_data="toggle_show_participants",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Показывать билеты: {_on_off(settings.get('show_tickets'))}",
                callback_data="toggle_show_tickets",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Кнопка «Поделиться»: {_on_off(settings.get('share_button'))}",
                callback_data="toggle_share",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Рейтинг приглашающих: {_on_off(settings.get('leaderboard'))}",
                callback_data="toggle_leaderboard",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Запасные победители: {settings.get('reserve_count', 0)}",
                callback_data="set_reserve",
            )
        ],
        [InlineKeyboardButton(text="✅ Завершить создание", callback_data="finish_contest")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")]]
    )
