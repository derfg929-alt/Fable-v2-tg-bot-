"""Пошаговый сценарий создания конкурса (FSM администратора).

Состояние шага хранится в JSON (admin_state[user_id] = {"step": ..., "temp_data": {...}}),
поэтому мастер не сбрасывается при перезапуске бота.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from . import database, keyboards

logger = logging.getLogger("fable_random_bot")

DEFAULT_SETTINGS: dict[str, Any] = {
    "tickets_per_ref": 5,
    "code_words": [],
    "code_word_tickets": 2,
    "sponsor_tasks": False,
    "sponsor_task_text": "",
    "show_participants": False,
    "show_tickets": False,
    "share_button": True,
    "share_text": "Участвую в розыгрыше! Присоединяйся:",
    "leaderboard": False,
    "leaderboard_size": 10,
    "reserve_count": 0,
    "reserve_timeout_hours": 24,
}


@dataclass
class Reply:
    text: str
    keyboard: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None


def _format_code_words(codes: list[Any]) -> str:
    parts = []
    for entry in codes:
        if isinstance(entry, dict):
            parts.append(f"{entry.get('word')}:{entry.get('tickets', 1)}")
        else:
            parts.append(str(entry))
    return ", ".join(parts) if parts else "выкл"


def _settings_summary(temp: dict[str, Any]) -> str:
    s = temp.get("settings", {})
    codes = s.get("code_words") or []
    return (
        "⚙️ Настройки конкурса\n"
        f"Название: {temp.get('title', '—')}\n"
        f"Приз: {temp.get('prize', '—')}\n"
        f"Канал: {temp.get('channel') or 'без канала (для всех)'}\n"
        f"Спонсоры: {', '.join(temp.get('sponsors', [])) or '—'}\n"
        f"Окончание: {temp.get('ends_at', '—')}\n"
        f"Победителей: {temp.get('winners_count', '—')}\n"
        f"Билетов за реферала: {s.get('tickets_per_ref')}\n"
        f"Кодовые слова: {_format_code_words(codes)}\n"
        f"Задания спонсоров: {'вкл' if s.get('sponsor_tasks') else 'выкл'}\n"
        f"Показывать участников: {'вкл' if s.get('show_participants') else 'выкл'}\n"
        f"Показывать билеты: {'вкл' if s.get('show_tickets') else 'выкл'}\n"
        f"Кнопка «Поделиться»: {'вкл' if s.get('share_button') else 'выкл'}\n"
        f"Рейтинг приглашающих: {'вкл' if s.get('leaderboard') else 'выкл'}\n"
        f"Запасные победители: {s.get('reserve_count')}\n\n"
        "Выбери, что изменить, или заверши создание."
    )


async def start_creation(user_id: int) -> Reply:
    temp = {"settings": dict(DEFAULT_SETTINGS), "sponsors": []}
    await database.set_admin_state(user_id, {"step": "waiting_title", "temp_data": temp})
    return Reply("Введи название конкурса:", keyboard=keyboards.cancel_keyboard())


async def _save_step(user_id: int, step: str, temp: dict[str, Any]) -> None:
    await database.set_admin_state(user_id, {"step": step, "temp_data": temp})


async def _validate_channel(
    bot: Bot, channel: str, user_id: int | None = None
) -> tuple[bool, str, int | None]:
    """Проверить канал для конкурса.

    Возвращает (ok, сообщение, chat_id). Публиковать конкурс в канале может
    только его владелец/администратор, и бот тоже должен быть администратором
    канала с правом публикации сообщений. Если ``user_id`` не передан, проверка
    владельца пропускается (используется для каналов-спонсоров, где нужен только
    доступ бота).
    """
    try:
        chat = await bot.get_chat(channel)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_chat(%s): %s", channel, exc)
        return False, "Не удалось найти канал. Проверь @username и попробуй снова.", None

    if user_id is not None:
        try:
            author = await bot.get_chat_member(chat_id=chat.id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_chat_member(user=%s, %s): %s", user_id, channel, exc)
            return (
                False,
                "Не удалось проверить твои права в канале. Убедись, что ты его "
                "владелец или администратор, и повтори.",
                None,
            )
        if author.status not in ("administrator", "creator"):
            return (
                False,
                "⛔ Ты не владелец и не администратор этого канала. Публиковать "
                "конкурс в канале может только его владелец/админ. Укажи свой "
                "канал или напиши «-», чтобы создать конкурс без канала.",
                None,
            )

    try:
        member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_chat_member(%s): %s", channel, exc)
        return False, "Не удалось проверить права бота. Добавь бота админом и повтори.", None
    if member.status not in ("administrator", "creator"):
        return False, "Бот не админ в этом канале. Добавь бота админом и повтори.", None
    if member.status == "administrator" and not getattr(member, "can_post_messages", True):
        return (
            False,
            "У бота нет права публиковать сообщения в канале. Включи для бота "
            "разрешение «Публикация сообщений» и повтори.",
            None,
        )
    return True, "", chat.id


def _parse_end_date(text: str) -> str | None:
    text = text.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return None


def _parse_code_words(text: str, default_tickets: int) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for chunk in text.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        word, _, tickets_raw = chunk.partition(":")
        if not tickets_raw:
            parts = chunk.split()
            word = parts[0]
            tickets_raw = parts[1] if len(parts) > 1 else ""
        word = word.strip().upper()
        try:
            tickets = int(tickets_raw) if tickets_raw else default_tickets
        except ValueError:
            tickets = default_tickets
        if word:
            words.append({"word": word, "tickets": tickets})
    return words


async def handle_text(user_id: int, text: str, bot: Bot) -> Reply | None:
    """Обработать текстовый ввод администратора в зависимости от шага."""
    state = await database.get_admin_state(user_id)
    step = state.get("step")
    temp = state.get("temp_data") or {}
    if not step:
        return None
    temp.setdefault("settings", dict(DEFAULT_SETTINGS))
    temp.setdefault("sponsors", [])
    text = (text or "").strip()

    if step == "waiting_title":
        temp["title"] = text
        await _save_step(user_id, "waiting_prize", temp)
        return Reply("Введи приз (что разыгрывается):", keyboard=keyboards.cancel_keyboard())

    if step == "waiting_prize":
        temp["prize"] = text
        await _save_step(user_id, "waiting_channel", temp)
        return Reply(
            "Укажи @username канала конкурса или нажми «🌍 Без канала (для всех)» — "
            "тогда участвовать смогут все:",
            keyboard=keyboards.channel_keyboard(),
        )

    if step == "waiting_channel":
        if text in ("-", "нет", "без канала", "пропустить", "skip", "—"):
            temp["channel"] = None
            temp["channel_id"] = None
            await _save_step(user_id, "waiting_sponsors", temp)
            return Reply(
                "🌍 Конкурс без канала — участвовать смогут все.\n\n"
                "Спонсоры конкурса (необязательно). Добавь спонсоров или нажми «Готово».",
                keyboard=keyboards.sponsors_keyboard(),
            )
        channel = text if text.startswith("@") else "@" + text
        ok, message, chat_id = await _validate_channel(bot, channel, user_id=user_id)
        if not ok:
            return Reply(message, keyboard=keyboards.channel_keyboard())
        temp["channel"] = channel
        temp["channel_id"] = chat_id
        await _save_step(user_id, "waiting_sponsors", temp)
        return Reply(
            "Спонсоры конкурса (необязательно). Добавь спонсоров или нажми «Готово».",
            keyboard=keyboards.sponsors_keyboard(),
        )

    if step == "waiting_sponsor_username":
        channel = text if text.startswith("@") else "@" + text
        ok, message, _ = await _validate_channel(bot, channel)
        if not ok:
            return Reply(message, keyboard=keyboards.cancel_keyboard())
        if channel not in temp["sponsors"]:
            temp["sponsors"].append(channel)
        await _save_step(user_id, "waiting_sponsors", temp)
        return Reply(
            f"Спонсор {channel} добавлен. Добавь ещё или нажми «Готово».",
            keyboard=keyboards.sponsors_keyboard(),
        )

    if step == "waiting_ends_at":
        parsed = _parse_end_date(text)
        if not parsed:
            return Reply(
                "Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (например, 31.12.2026 23:59).",
                keyboard=keyboards.cancel_keyboard(),
            )
        temp["ends_at"] = parsed
        await _save_step(user_id, "waiting_winners_count", temp)
        return Reply("Сколько победителей?", keyboard=keyboards.cancel_keyboard())

    if step == "waiting_winners_count":
        if not text.isdigit() or int(text) < 1:
            return Reply("Введи целое число победителей (1 или больше).")
        temp["winners_count"] = int(text)
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))

    if step == "waiting_ref_tickets":
        if not text.isdigit():
            return Reply("Введи целое число билетов за реферала.")
        temp["settings"]["tickets_per_ref"] = int(text)
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))

    if step == "waiting_code_words":
        words = _parse_code_words(text, temp["settings"].get("code_word_tickets", 2))
        if not words:
            return Reply("Не понял кодовые слова. Пример: PROMO2026:2, SALE:3")
        temp["settings"]["code_words"] = words
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))

    if step == "waiting_task_text":
        temp["settings"]["sponsor_task_text"] = text
        temp["settings"]["sponsor_tasks"] = True
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))

    if step == "waiting_share_text":
        temp["settings"]["share_text"] = text
        temp["settings"]["share_button"] = True
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))

    if step == "waiting_leaderboard_size":
        if not text.isdigit() or int(text) < 1:
            return Reply("Введи целое число мест в топе.")
        temp["settings"]["leaderboard_size"] = int(text)
        temp["settings"]["leaderboard"] = True
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))

    if step == "waiting_reserve":
        if not text.isdigit():
            return Reply("Введи целое число запасных победителей (0 или больше).")
        temp["settings"]["reserve_count"] = int(text)
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))

    return None


async def handle_callback(user_id: int, data: str, bot: Bot) -> Reply | None:
    """Обработать inline-кнопки мастера (спонсоры и настройки)."""
    state = await database.get_admin_state(user_id)
    step = state.get("step")
    temp = state.get("temp_data") or {}
    if not step:
        return None
    temp.setdefault("settings", dict(DEFAULT_SETTINGS))
    temp.setdefault("sponsors", [])
    settings = temp["settings"]

    if data == "cancel":
        await database.clear_admin_state(user_id)
        return Reply("Создание конкурса отменено.")

    if data == "skip_channel":
        if step != "waiting_channel":
            return None
        temp["channel"] = None
        temp["channel_id"] = None
        await _save_step(user_id, "waiting_sponsors", temp)
        return Reply(
            "🌍 Конкурс без канала — участвовать смогут все.\n\n"
            "Спонсоры конкурса (необязательно). Добавь спонсоров или нажми «Готово».",
            keyboard=keyboards.sponsors_keyboard(),
        )

    if data == "add_sponsor":
        await _save_step(user_id, "waiting_sponsor_username", temp)
        return Reply("Пришли @username канала-спонсора:", keyboard=keyboards.cancel_keyboard())

    if data == "sponsors_done":
        await _save_step(user_id, "waiting_ends_at", temp)
        return Reply(
            "Когда завершить конкурс? Формат: ДД.ММ.ГГГГ ЧЧ:ММ",
            keyboard=keyboards.cancel_keyboard(),
        )

    if step != "settings":
        return None

    if data == "set_ref_tickets":
        await _save_step(user_id, "waiting_ref_tickets", temp)
        return Reply("Сколько билетов начислять за каждого приглашённого?")
    if data == "toggle_codes":
        if settings.get("code_words"):
            settings["code_words"] = []
            await _save_step(user_id, "settings", temp)
            return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))
        await _save_step(user_id, "waiting_code_words", temp)
        return Reply("Пришли кодовые слова и билеты: PROMO2026:2, SALE:3")
    if data == "toggle_tasks":
        if settings.get("sponsor_tasks"):
            settings["sponsor_tasks"] = False
            await _save_step(user_id, "settings", temp)
            return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))
        await _save_step(user_id, "waiting_task_text", temp)
        return Reply("Пришли текст задания спонсора:")
    if data == "toggle_show_participants":
        settings["show_participants"] = not settings.get("show_participants")
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))
    if data == "toggle_show_tickets":
        settings["show_tickets"] = not settings.get("show_tickets")
        await _save_step(user_id, "settings", temp)
        return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))
    if data == "toggle_share":
        if settings.get("share_button"):
            settings["share_button"] = False
            await _save_step(user_id, "settings", temp)
            return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))
        await _save_step(user_id, "waiting_share_text", temp)
        return Reply("Пришли текст для кнопки «Поделиться»:")
    if data == "toggle_leaderboard":
        if settings.get("leaderboard"):
            settings["leaderboard"] = False
            await _save_step(user_id, "settings", temp)
            return Reply(_settings_summary(temp), keyboard=keyboards.settings_keyboard(temp))
        await _save_step(user_id, "waiting_leaderboard_size", temp)
        return Reply("Сколько мест показывать в рейтинге приглашающих?")
    if data == "set_reserve":
        await _save_step(user_id, "waiting_reserve", temp)
        return Reply("Сколько запасных победителей? (0 — без запасных)")

    if data == "finish_contest":
        return await _finish(user_id, temp, bot)

    return None


async def _finish(user_id: int, temp: dict[str, Any], bot: Bot) -> Reply:
    data = await database.load_data()
    contest_id = database.next_contest_id(data)
    contest = {
        "id": contest_id,
        "title": temp.get("title", "Конкурс"),
        "prize": temp.get("prize", ""),
        "owner_id": user_id,
        "channel": temp.get("channel"),
        "channel_id": temp.get("channel_id"),
        "sponsors": temp.get("sponsors", []),
        "status": "active",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "ends_at": temp.get("ends_at"),
        "winners_count": temp.get("winners_count", 1),
        "participants": [],
        "winners": [],
        "reserve_winners": [],
        "settings": temp.get("settings", dict(DEFAULT_SETTINGS)),
    }
    await database.create_contest(contest)
    await database.clear_admin_state(user_id)
    database.invalidate_leaderboard_cache(contest_id)

    published = await _publish_contest(bot, contest)
    if not contest.get("channel_id"):
        suffix = "\n🌍 Конкурс без канала — участвовать могут все."
    elif published:
        suffix = ""
    else:
        suffix = "\n⚠️ Не удалось опубликовать пост в канал — проверь права бота."
    return Reply(
        f"✅ Конкурс «{contest['title']}» создан (id {contest_id}).{suffix}",
        keyboard=keyboards.main_menu(user_id),
    )


async def _publish_contest(bot: Bot, contest: dict[str, Any]) -> bool:
    channel_id = contest.get("channel_id")
    if not channel_id:
        return False
    text = (
        f"🎉 Розыгрыш: {contest['title']}\n"
        f"🎁 Приз: {contest.get('prize', '')}\n"
        f"⏰ Итоги: {contest.get('ends_at', '')}\n\n"
        "Нажми «Участвовать», чтобы принять участие!"
    )
    try:
        await bot.send_message(
            chat_id=channel_id,
            text=text,
            reply_markup=keyboards.join_keyboard(contest["id"]),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось опубликовать конкурс %s: %s", contest["id"], exc)
        return False
