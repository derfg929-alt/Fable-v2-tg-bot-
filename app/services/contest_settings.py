"""Гибкие настройки конкурса Fable Randomizer (ТЗ часть 6 и 9.2).

Настройки конкурса хранятся **строго в JSON** — в колонке `contests.settings_json`
(тип JSONB/JSON). Никаких других форматов: это единый объект со всеми
переключателями, описанными в ТЗ. Функции ниже нормализуют, читают и обновляют
этот объект, гарантируя наличие всех ключей и корректные типы/диапазоны.

Список настроек (ТЗ 6.1–6.8) и значения по умолчанию:
  code_words        — вкл/выкл кодовые слова           (по умолчанию выкл)
  code_words_list   — [{word, tickets}]                (пусто)
  code_words_multi  — можно вводить несколько слов     (True)
  sponsor_tasks     — вкл/выкл задания спонсоров       (выкл)
  sponsor_task_text — текст задания                     ("")
  sponsor_task_tickets — сколько билетов за задание     (1)
  tickets_per_ref   — билетов за 1 реферала             (1)
  show_participants — показывать список участников      (вкл)
  show_tickets      — показывать счётчик билетов        (выкл)
  share_button      — кнопка «Поделиться»               (вкл)
  share_text        — текст для шаринга                 (стандартный)
  leaderboard       — рейтинг приглашающих              (выкл)
  leaderboard_size  — сколько мест в топе               (10)
  sources_report    — отчёт по источникам (только админ)(вкл)
  reserve_winners   — запасных победителей              (0)
  reserve_timeout_hours — часов на связь с победителем  (24)
"""
from __future__ import annotations

import copy
from typing import Any

# Ключи, которые обязаны присутствовать в settings_json.
DEFAULT_SETTINGS: dict[str, Any] = {
    "code_words": False,
    "code_words_list": [],
    "code_words_multi": True,
    "sponsor_tasks": False,
    "sponsor_task_text": "",
    "sponsor_task_tickets": 1,
    "tickets_per_ref": 1,
    "show_participants": True,
    "show_tickets": False,
    "share_button": True,
    "share_text": "",
    "leaderboard": False,
    "leaderboard_size": 10,
    "sources_report": True,
    "reserve_winners": 0,
    "reserve_timeout_hours": 24,
}

# Человекочитаемые подписи для переключателей (бот/админка).
TOGGLE_LABELS: dict[str, str] = {
    "code_words": "🔑 Кодовые слова",
    "sponsor_tasks": "🎯 Задания от спонсоров",
    "show_participants": "👥 Показывать список участников",
    "show_tickets": "🎟 Показывать счётчик билетов",
    "share_button": "📤 Кнопка «Поделиться»",
    "leaderboard": "🏅 Рейтинг приглашающих",
    "sources_report": "📈 Отчёт по источникам приглашений",
    "code_words_multi": "🔢 Несколько кодовых слов",
}

# Порядок переключателей для отображения в мастере создания конкурса.
TOGGLE_ORDER = [
    "code_words",
    "sponsor_tasks",
    "show_participants",
    "show_tickets",
    "share_button",
    "leaderboard",
    "sources_report",
]


def default_settings() -> dict[str, Any]:
    """Возвращает глубокую копию настроек по умолчанию."""
    return copy.deepcopy(DEFAULT_SETTINGS)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "вкл", "да")
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and result < minimum:
        return minimum
    return result


def _normalize_code_words(raw: Any) -> list[dict]:
    words: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                word = str(item.get("word") or "").strip()
                if not word:
                    continue
                words.append({"word": word, "tickets": _as_int(item.get("tickets"), 1, minimum=1)})
            elif isinstance(item, str) and item.strip():
                words.append({"word": item.strip(), "tickets": 1})
    return words


def normalize_settings(raw: Any) -> dict[str, Any]:
    """Приводит произвольный объект к полной, типобезопасной схеме настроек.

    Всегда возвращает JSON-объект (dict) со всеми ключами DEFAULT_SETTINGS.
    """
    if not isinstance(raw, dict):
        return default_settings()

    result = default_settings()
    for key in ("code_words", "sponsor_tasks", "show_participants", "show_tickets",
                "share_button", "leaderboard", "sources_report", "code_words_multi"):
        if key in raw:
            result[key] = _as_bool(raw[key], result[key])

    result["code_words_list"] = _normalize_code_words(raw.get("code_words_list"))
    result["sponsor_task_text"] = str(raw.get("sponsor_task_text") or "").strip()
    result["sponsor_task_tickets"] = _as_int(raw.get("sponsor_task_tickets"), 1, minimum=1)
    result["tickets_per_ref"] = _as_int(raw.get("tickets_per_ref"), 1, minimum=1)
    result["share_text"] = str(raw.get("share_text") or "").strip()
    result["leaderboard_size"] = _as_int(raw.get("leaderboard_size"), 10, minimum=1)
    result["reserve_winners"] = _as_int(raw.get("reserve_winners"), 0, minimum=0)
    result["reserve_timeout_hours"] = _as_int(raw.get("reserve_timeout_hours"), 24, minimum=1)
    return result


def read_settings(contest) -> dict[str, Any]:
    """Возвращает нормализованные настройки конкурса (из settings_json)."""
    raw = getattr(contest, "settings_json", None)
    return normalize_settings(raw)


def toggle(settings: dict[str, Any], key: str) -> dict[str, Any]:
    """Переключает булев флаг в настройках и возвращает новый объект."""
    result = normalize_settings(settings)
    if key in result and isinstance(result[key], bool):
        result[key] = not result[key]
    return result


def default_share_text(prize: str | None, link: str) -> str:
    """Стандартный текст кнопки «Поделиться» (используется, если админ не задал свой)."""
    prize_line = f"«{prize}»" if prize else "розыгрыше призов"
    return f"Я участвую в {prize_line}! Присоединяйся по моей ссылке — тебе тоже дадут билеты: {link}"


def public_summary(settings: dict[str, Any]) -> list[str]:
    """Строки с описанием включённых механик — для карточки конкурса участнику."""
    settings = normalize_settings(settings)
    lines: list[str] = []
    if settings["code_words"] and settings["code_words_list"]:
        words = ", ".join(w["word"] for w in settings["code_words_list"])
        lines.append(f"🔑 Кодовые слова: {words}")
    if settings["sponsor_tasks"] and settings["sponsor_task_text"]:
        lines.append(f"🎯 Задание спонсора: {settings['sponsor_task_text']}")
    if settings["tickets_per_ref"] > 1:
        lines.append(f"👥 За 1 приглашённого друга: +{settings['tickets_per_ref']} билет(ов)")
    if settings["leaderboard"]:
        lines.append(f"🏅 Рейтинг приглашающих: топ-{settings['leaderboard_size']}")
    return lines
