"""Обработчики развлекательных RP-команд и мини-игр Fable.

- RP-взаимодействия: обнять, поцеловать, ударить, дать пять, погладить, укусить,
  тыкнуть, шлёпнуть, обнять всех, комплимент.
- Мини-игры и гадания: кубик [NdM], шип (совместимость), правда/действие, ктоя.

Команды не пересекаются с игровыми/экономическими (кости, рулетка, шар и т.д.),
поэтому регистрируются отдельным роутером после игрового.
"""
from __future__ import annotations

import re

from aiogram import Router, types, F

from app.db.session import session_scope
from app.db.models import Player
from app.services import rp_service
from app.services.player_service import get_or_create_player

router = Router(name="rp_router")


async def _resolve_target(message: types.Message, session, arg: str | None) -> tuple[int | None, str | None]:
    """Возвращает (telegram_id, имя) цели по reply / @username / текстовому упоминанию / ID."""
    reply = getattr(message, "reply_to_message", None)
    if reply is not None and getattr(reply, "from_user", None):
        u = reply.from_user
        return u.id, (u.first_name or "пользователь")

    entities = getattr(message, "entities", None)
    if isinstance(entities, (list, tuple)):
        for ent in entities:
            if getattr(ent, "type", None) == "text_mention" and getattr(ent, "user", None):
                return ent.user.id, (ent.user.first_name or "пользователь")

    if arg:
        m = re.search(r"@([a-zA-Z0-9_]+)", arg)
        if m:
            uname = m.group(1).lower()
            p = await session.scalar(select_player_by_username(uname))
            if p:
                return p.telegram_id, (p.custom_nickname or p.first_name or "пользователь")
            return None, None
        parts = arg.split()
        for part in parts:
            if part.isdigit() and len(part) >= 6:
                tid = int(part)
                p = await session.get(Player, tid)
                if p:
                    return tid, (p.custom_nickname or p.first_name or "пользователь")
                return tid, None
    return None, None


def select_player_by_username(uname: str):
    from sqlalchemy import func, select

    return select(Player).where(func.lower(Player.username) == uname)


# ===================== RP-ДЕЙСТВИЯ =====================

_RP_WORDS = sorted(set(list(rp_service.RP_ACTIONS.keys()) + list(rp_service.RP_ALIASES.keys())), key=len, reverse=True)
_RP_RE = re.compile(r"^(" + "|".join(re.escape(w) for w in _RP_WORDS) + r")(?:\s+(.+))?$", re.IGNORECASE)


@router.message(F.text.func(lambda t: bool(_RP_RE.match((t or "").strip()))))
async def handle_rp_action(message: types.Message):
    if not message.chat or not message.from_user:
        return
    parsed = rp_service.parse_rp_command(message.text or "")
    if not parsed:
        return
    action, arg = parsed

    # «обнять всех» -> массовое объятие
    if action == "обнять" and arg and arg.strip().casefold() in ("всех", "все"):
        action = "обнятьвсех"
        arg = None

    actor_name = message.from_user.first_name or "Игрок"

    async with session_scope() as session:
        await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=actor_name,
            username=message.from_user.username,
        )
        target_name = None
        if action != "обнятьвсех":
            _, target_name = await _resolve_target(message, session, arg)

    text = rp_service.build_rp_text(action, actor_name, target_name)
    await message.reply(text, parse_mode="HTML")


# ===================== КУБИК =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in ["кубик", "куб", "!кубик", "/roll", "roll", "бросок"])))
async def handle_roll(message: types.Message):
    parts = (message.text or "").split()
    count, sides = 1, 6
    if len(parts) >= 2:
        token = parts[1].casefold()
        m = re.match(r"^(\d+)?d(\d+)$", token)
        if m:
            count = int(m.group(1)) if m.group(1) else 1
            sides = int(m.group(2))
        elif token.isdigit():
            sides = int(token)
    await message.reply(rp_service.roll_dice_text(count, sides), parse_mode="HTML")


# ===================== ШИП (СОВМЕСТИМОСТЬ) =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in ["шип", "!шип", "/ship", "ship", "совместимость"])))
async def handle_ship(message: types.Message):
    if not message.from_user:
        return
    arg = (message.text or "").split(maxsplit=1)
    arg = arg[1] if len(arg) > 1 else None

    async with session_scope() as session:
        a_id = message.from_user.id
        a_name = message.from_user.first_name or "Игрок"
        b_id, b_name = await _resolve_target(message, session, arg)

    if not b_id or not b_name:
        await message.reply("💞 Использование: ответом на сообщение или <code>шип @user</code>", parse_mode="HTML")
        return
    await message.reply(
        rp_service.ship_text(a_name, b_name, str(a_id), str(b_id)),
        parse_mode="HTML",
    )


# ===================== ПРАВДА ИЛИ ДЕЙСТВИЕ =====================

@router.message(F.text.casefold().in_([
    "правда действие", "правда или действие", "пди", "!пди", "/tod", "правдаилидействие",
]))
async def handle_truth_or_dare(message: types.Message):
    await message.reply(rp_service.truth_or_dare_text(), parse_mode="HTML")


# ===================== КОМПЛИМЕНТ =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in ["комплимент", "!комплимент", "/compliment", "похвали"])))
async def handle_compliment(message: types.Message):
    if not message.from_user:
        return
    arg = (message.text or "").split(maxsplit=1)
    arg = arg[1] if len(arg) > 1 else None

    async with session_scope() as session:
        _, target_name = await _resolve_target(message, session, arg)
    name = target_name or (message.from_user.first_name or "Игрок")
    await message.reply(rp_service.compliment_text(name), parse_mode="HTML")


# ===================== КТО Я (ВЕСЁЛАЯ КАРТОЧКА) =====================

_WHO_I_AM = (
    "сегодня ты — <b>неоспоримый лидер</b> 👑",
    "сегодня ты — <b>душа компании</b> 🎉",
    "сегодня ты — <b>гений математики</b> 🧮",
    "сегодня ты — <b>мастер загадок</b> 🕵️",
    "сегодня ты — <b>звезда вечеринок</b> ⭐️",
    "сегодня ты — <b>укротитель удачи</b> 🍀",
    "сегодня ты — <b>легенда этого чата</b> 🔥",
    "сегодня ты — <b>тихий мудрец</b> 🧘",
)


@router.message(F.text.casefold().in_(["ктоя", "кто я", "!ктоя", "кто я?"]))
async def handle_who_am_i(message: types.Message):
    import random

    if not message.from_user:
        return
    name = message.from_user.first_name or "Игрок"
    await message.reply(f"🔮 <b>{name}</b>, {random.choice(_WHO_I_AM)}", parse_mode="HTML")


# ===================== ПОЛЕЗНЫЕ УТИЛИТЫ =====================

@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in ["рандом", "!рандом", "/random"])))
async def handle_random_number(message: types.Message):
    """Случайное число в диапазоне: 'рандом 1 100'."""
    import random

    parts = (message.text or "").split()
    nums = [int(p) for p in parts[1:] if p.lstrip("-").isdigit()]
    if len(nums) >= 2:
        lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
    elif len(nums) == 1:
        lo, hi = 1, nums[0]
    else:
        lo, hi = 1, 100
    if hi - lo > 10_000_000:
        await message.reply("❌ Слишком большой диапазон.")
        return
    await message.reply(f"🎲 Случайное число от {lo} до {hi}: <b>{random.randint(lo, hi)}</b>", parse_mode="HTML")


@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in ["выбор", "!выбор", "/choose"])))
async def handle_choose(message: types.Message):
    """Выбор из вариантов: 'выбор пицца или суши или бургер'."""
    import random

    text = (message.text or "").strip()
    body = text.split(maxsplit=1)
    body = body[1] if len(body) > 1 else ""
    options = [o.strip() for o in re.split(r"\s+или\s+|\||,", body) if o.strip()]
    if len(options) < 2:
        await message.reply("🤔 Укажите варианты: <code>выбор пицца или суши или бургер</code>", parse_mode="HTML")
        return
    choice = random.choice(options)
    await message.reply(f"🎯 Мой выбор: <b>{choice}</b>", parse_mode="HTML")


@router.message(F.text.casefold().in_(["пинг", "!пинг", "/ping", "бот жив", "жив"]))
async def handle_ping(message: types.Message):
    import time

    started = time.perf_counter()
    sent = await message.reply("🏓 Пингую...")
    latency_ms = (time.perf_counter() - started) * 1000
    try:
        await sent.edit_text(f"🏓 Понг! Задержка: <b>{latency_ms:.0f} мс</b>", parse_mode="HTML")
    except Exception:
        pass

