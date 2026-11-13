"""Сервис развлекательных RP-команд, мини-игр и информационных карточек Fable.

Содержит:
- RP-взаимодействия: обнять, поцеловать, ударить, дать пять, погладить, укусить,
  обнять всех, комплимент, «тык» (poke).
- Мини-игры и гадания: кубик (roll), 8-шар, орёл/решка (псевдоним), «правда/действие».
- Информационные карточки: «шип» (ship) совместимость, «профиль/кто я», «инфо» игрока.

Все функции чистые и детерминированы там, где это важно (ship по хэшу id),
чтобы не зависеть от случайности и не «прыгать» при повторных вызовах.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass

RP_ACTIONS: dict[str, dict] = {
    "обнять": {"verb": "крепко обнимает", "emoji": "🤗", "self": "обнимает всех вокруг"},
    "поцеловать": {"verb": "нежно целует", "emoji": "😘", "self": "посылает воздушный поцелуй"},
    "ударить": {"verb": "шутливо бьёт", "emoji": "👋", "self": "бьёт себя по лбу"},
    "датьпять": {"verb": "даёт пять", "emoji": "🙌", "self": "даёт пять самому себе"},
    "погладить": {"verb": "гладит по голове", "emoji": "🫶", "self": "гладит себя по голове"},
    "укусить": {"verb": "шутливо кусает", "emoji": "🧛", "self": "кусает себя за локоть"},
    "тыкнуть": {"verb": "тыкает пальцем в", "emoji": "👉", "self": "тыкает пальцем в себя"},
    "шлёпнуть": {"verb": "шлёпает", "emoji": "🍑", "self": "шлёпает себя"},
    "обнятьвсех": {"verb": "обнимает всех присутствующих", "emoji": "🤗", "self": ""},
}

RP_ALIASES: dict[str, str] = {
    "хаг": "обнять", "hug": "обнять", "обнимаю": "обнять",
    "кис": "поцеловать", "kiss": "поцеловать", "чмок": "поцеловать",
    "слэп": "шлёпнуть", "slap": "шлёпнуть",
    "хайфайв": "датьпять", "highfive": "датьпять", "дать пять": "датьпять", "хай файв": "датьпять",
    "покус": "укусить", "bite": "укусить",
    "poke": "тыкнуть", "тык": "тыкнуть",
    "пэт": "погладить", "pat": "погладить",
    "обнять всех": "обнятьвсех", "обнять все": "обнятьвсех", "hug all": "обнятьвсех",
}

DICE_EMOJI = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}

TRUTH_OR_DARE = (
    "🔥 Правда: Назови свой самый неловкий момент за последний год.",
    "🔥 Правда: Какой фильм заставил тебя плакать?",
    "🔥 Правда: Расскажи о своей самой большой фобии.",
    "🔥 Правда: Какое сообщение ты отправлял и сразу удалял?",
    "🔥 Правда: Признайся, что ты гуглил последним.",
    "🎯 Действие: Отправь в чат свой последний селфи или опиши его словами.",
    "🎯 Действие: Напиши комплимент предыдущему отправителю в чате.",
    "🎯 Действие: Изобрази любимое животное в одном сообщении.",
    "🎯 Действие: Отправь случайный стикер без объяснений.",
    "🎯 Действие: Расскажи анекдот. Если никто не засмеётся — расскажи ещё один!",
)

COMPLIMENTS = (
    "у тебя отличное чувство юмора 😄",
    "ты делаешь этот чат ярче ✨",
    "с тобой всегда интересно общаться 💬",
    "ты очень сообразительный(ая) 🧠",
    "у тебя доброе сердце ❤️",
    "ты — настоящая находка для этого чата 💎",
    "с тобой не соскучишься 🎉",
    "ты умеешь поддержать в нужный момент 🤝",
    "у тебя отличный вкус 🎩",
    "ты заряжаешь позитивом ⚡️",
)

SHIP_PHRASES = (
    (95, "Идеальная пара! Свадьбу играем прямо завтра 💍"),
    (85, "Огонь! Между вами настоящая искра 🔥"),
    (70, "Отличная совместимость, стоит присмотреться 💕"),
    (55, "Есть потенциал — попробуйте узнать друг друга ближе 🙂"),
    (40, "Так себе союз, но дружба точно получится 🤝"),
    (25, "Звёзды говорят: лучше остаться друзьями 🌚"),
    (10, "Совсем не пара, но посмеяться можно 😅"),
    (0, "Корабль разбился о скалы реальности 🌊"),
)


@dataclass(frozen=True, slots=True)
class RpResult:
    text: str


def _hash_percent(*parts: str) -> int:
    """Детерминированный процент 0..100 на основе строк (стабильный результат)."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 101


def normalize_rp_command(word: str) -> str | None:
    """Приводит ввод пользователя к каноничной RP-команде."""
    w = word.strip().casefold()
    if w in RP_ACTIONS:
        return w
    return RP_ALIASES.get(w)


def build_rp_text(action: str, actor_name: str, target_name: str | None) -> str:
    """Формирует текст RP-действия."""
    cfg = RP_ACTIONS.get(action)
    if not cfg:
        return "🤔 Неизвестное действие."
    if action == "обнятьвсех":
        return f"{cfg['emoji']} <b>{actor_name}</b> {cfg['verb']}!"
    if not target_name:
        if not cfg["self"]:
            return f"{cfg['emoji']} <b>{actor_name}</b> {cfg['verb']}!"
        return f"{cfg['emoji']} <b>{actor_name}</b> {cfg['self']}!"
    return f"{cfg['emoji']} <b>{actor_name}</b> {cfg['verb']} <b>{target_name}</b>!"


def roll_dice_text(count: int, sides: int) -> str:
    """Бросок N кубиков с S гранями. Возвращает готовый HTML-текст."""
    count = max(1, min(count, 10))
    sides = max(2, min(sides, 1000))
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)
    if sides == 6:
        shown = " ".join(DICE_EMOJI.get(r, str(r)) for r in rolls)
    else:
        shown = " + ".join(str(r) for r in rolls)
    return f"🎲 <b>Бросок {count}d{sides}</b>\n{shown}\nСумма: <b>{total}</b>"


def magic_ball_text(question: str) -> str:
    from app.services.social_fun_service import get_magic_ball_prediction

    return get_magic_ball_prediction(question)


def truth_or_dare_text() -> str:
    return random.choice(TRUTH_OR_DARE)


def compliment_text(target_name: str) -> str:
    return f"💐 <b>{target_name}</b>, {random.choice(COMPLIMENTS)}"


def ship_text(name_a: str, name_b: str, id_a: str, id_b: str) -> str:
    """Совместимость двух игроков. Результат детерминирован для пары id."""
    lo, hi = sorted([str(id_a), str(id_b)])
    percent = _hash_percent(lo, hi, name_a.casefold(), name_b.casefold())
    phrase = next(p for threshold, p in SHIP_PHRASES if percent >= threshold)
    bar_len = 10
    filled = round(percent / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    return (
        f"💞 <b>Совместимость</b>\n"
        f"{name_a} ❤️ {name_b}\n\n"
        f"{bar} <b>{percent}%</b>\n\n{phrase}"
    )


_ACTION_WORDS = sorted(set(list(RP_ACTIONS.keys()) + list(RP_ALIASES.keys())), key=len, reverse=True)
RP_TRIGGER_RE = re.compile(r"^(" + "|".join(re.escape(w) for w in _ACTION_WORDS) + r")(?:\s+(.+))?$", re.IGNORECASE)


def parse_rp_command(text: str) -> tuple[str, str | None] | None:
    """Разбирает текст на (каноничная_команда, аргумент). None, если не RP-команда."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    m = RP_TRIGGER_RE.match(stripped)
    if not m:
        return None
    canonical = normalize_rp_command(m.group(1))
    if not canonical:
        return None
    arg = (m.group(2) or "").strip() or None
    return canonical, arg
