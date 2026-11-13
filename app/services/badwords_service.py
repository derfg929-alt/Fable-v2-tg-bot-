"""Сервис фильтра нецензурной лексики (антимат) для Fable.

По умолчанию фильтр выключен — администратор включает его командой
"антимат вкл". Словарь намеренно компактный и расширяемый: при необходимости
администратор может добавлять свои слова через "антимат добавить <слово>".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatBadWord

# Базовый русскоязычный словарь (корни/леммы). Хранится в нижнем регистре.
DEFAULT_BAD_WORDS: tuple[str, ...] = (
    "бля", "блят", "блядь", "блять", "хуй", "хуе", "хуё", "хуя", "хую", "хуи",
    "пизд", "пизде", "пизда", "пиздец", "ебат", "ебал", "ебан", "ебуч", "еби",
    "ебет", "ебёт", "заеб", "выеб", "наеб", "отъеб", "уеб", "съеб",
    "сука", "суки", "сучар", "мудак", "мудил", "мудач", "гандон", "гондон",
    "долбоеб", "долбоёб", "пидор", "пидар", "педик", "шлюх", "манда", "мандавош",
    "жоп", "жопa", "срак", "говн", "дерьм", "нахер", "нахуй", "охуе", "охуи",
    "ахуе", "нехуй", "похуй", "нахуя", "залуп", "шмара", "блуд", "курв",
    "fuck", "fuk", "shit", "bitch", "cunt", "dick", "asshole", "bastard",
    "whore", "slut", "nigg", "faggot", "pussy", "motherfuck",
)

# Группы символов-«двойников» (латиница/кириллица/цифры), которые нарушители
# используют для обхода фильтра. Каждый символ группы эквивалентен остальным.
_EQUIV_GROUPS: tuple[str, ...] = (
    "aа4@", "eе", "oо0", "cс$", "pр", "xх", "yу", "kк", "mм", "tт", "hн", "bв6", "иi1", "3з",
)

# Карта: символ -> класс символов, которые его заменяют (включая сам символ).
_CHAR_CLASS: dict[str, str] = {}
for _group in _EQUIV_GROUPS:
    for _ch in _group:
        _CHAR_CLASS[_ch] = _group

# Разделители между буквами, которые нарушители вставляют для обхода фильтра.
_SEPARATOR_CLASS = r"[^\w]*"

# Кэш скомпилированных регулярных выражений для каждого запрещённого слова.
_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


class BadWordsError(Exception):
    """Базовая ошибка фильтра нецензурной лексики."""


@dataclass(frozen=True, slots=True)
class BadWordsHit:
    word: str
    action: str
    duration_seconds: int


def _normalize(text: str) -> str:
    """Нижний регистр и сворачивание повторяющихся букв (fuuuck -> fuck)."""
    return re.sub(r"(.)\1{1,}", r"\1", text.lower())


def _pattern_for(bad: str) -> re.Pattern[str]:
    """Регулярное выражение для слова с допуском разделителей и символов-двойников."""
    cached = _PATTERN_CACHE.get(bad)
    if cached is not None:
        return cached
    classes: list[str] = []
    for ch in bad:
        group = _CHAR_CLASS.get(ch)
        if group:
            classes.append("[" + re.escape(group) + "]")
        else:
            classes.append(re.escape(ch))
    pattern = re.compile(_SEPARATOR_CLASS.join(classes))
    _PATTERN_CACHE[bad] = pattern
    return pattern


def find_bad_word(text: str, *, extra_words: list[str] | None = None) -> str | None:
    """Возвращает первое найденное запрещённое слово или None."""
    if not text:
        return None
    normalized = _normalize(text)
    if not normalized:
        return None
    words = list(DEFAULT_BAD_WORDS) + [w.lower() for w in (extra_words or [])]
    for bad in words:
        if not bad:
            continue
        if _pattern_for(bad).search(normalized):
            return bad
    return None


async def get_custom_bad_words(session: AsyncSession, *, chat_id: int) -> list[str]:
    """Возвращает добавленные администратором слова чата."""
    rows = await session.scalars(
        select(ChatBadWord.word).where(ChatBadWord.chat_id == chat_id)
    )
    return list(rows.all())


async def add_custom_bad_word(
    session: AsyncSession, *, chat_id: int, word: str
) -> str:
    """Добавляет слово в персональный фильтр чата (идемпотентно)."""
    clean = word.strip().lower()
    if len(clean) < 2:
        raise BadWordsError("Слово должно быть длиной от 2 символов")
    existing = await session.scalar(
        select(ChatBadWord).where(
            ChatBadWord.chat_id == chat_id, ChatBadWord.word == clean
        )
    )
    if existing:
        return clean
    session.add(ChatBadWord(chat_id=chat_id, word=clean))
    await session.flush()
    return clean


async def remove_custom_bad_word(
    session: AsyncSession, *, chat_id: int, word: str
) -> bool:
    """Удаляет слово из персонального фильтра чата."""
    clean = word.strip().lower()
    row = await session.scalar(
        select(ChatBadWord).where(
            ChatBadWord.chat_id == chat_id, ChatBadWord.word == clean
        )
    )
    if not row:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def check_message(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str | None,
    settings: dict | None = None,
) -> BadWordsHit | None:
    """Проверяет сообщение на нецензурную лексику с учётом настроек чата."""
    settings = settings or {}
    if not settings.get("badwords_enabled", False):
        return None
    if not text:
        return None
    extra = await get_custom_bad_words(session, chat_id=chat_id)
    found = find_bad_word(text, extra_words=extra)
    if not found:
        return None
    action = str(settings.get("badwords_action", "delete"))
    duration = int(settings.get("badwords_duration_seconds", 600))
    return BadWordsHit(word=found, action=action, duration_seconds=duration)
