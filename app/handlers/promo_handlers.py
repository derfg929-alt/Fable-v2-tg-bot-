"""Обработчики промокодов Fable:
- "+промо КОД МОНЕТЫ [РЕЙТИНГ] [ЛИМИТ] [НА_ЮЗЕРА]" — создание промокода (только владелец).
- "промокоды" / "промо список" — список промокодов (только владелец).
- "промо КОД" — активация промокода игроком (в ЛС и в группах).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.db.session import session_scope
from app.services.promo_service import (
    PromoError,
    activate_promo,
    create_promo,
    delete_promo,
    get_promo,
    list_promos,
    set_promo_active,
)
from app.services.moderation_service import parse_duration_string

router = Router(name="promo_router")


def _is_owner(user_id: int) -> bool:
    return user_id == 8039111975 or user_id == get_settings().owner_id


def _parse_create_args(parts: list[str]) -> dict:
    """Разбирает аргументы создания промокода: позиционные и именованные."""
    # parts[0] == '+промо', parts[1] == код
    code = parts[1] if len(parts) > 1 else ""
    coins = 0
    rating = 0
    max_activations = 0
    per_user_limit = 1
    expires_at: datetime | None = None

    positional = [p for p in parts[2:] if p.isdigit()]
    if positional:
        coins = int(positional[0])
    if len(positional) > 1:
        rating = int(positional[1])
    if len(positional) > 2:
        max_activations = int(positional[2])
    if len(positional) > 3:
        per_user_limit = max(1, int(positional[3]))

    low = [p.casefold() for p in parts]
    for i, token in enumerate(low):
        if token in ("рейтинг", "rating") and i + 1 < len(low) and low[i + 1].isdigit():
            rating = int(low[i + 1])
        if token in ("лимит", "limit") and i + 1 < len(low) and low[i + 1].isdigit():
            max_activations = int(low[i + 1])
        if token in ("на_юзера", "наюзера", "юзера") and i + 1 < len(low) and low[i + 1].isdigit():
            per_user_limit = max(1, int(low[i + 1]))
        if token in ("срок", "дней") and i + 1 < len(parts):
            try:
                expires_at = datetime.now(timezone.utc) + parse_duration_string(parts[i + 1])
            except Exception:
                pass

    return {
        "code": code,
        "coins": coins,
        "rating": rating,
        "max_activations": max_activations,
        "per_user_limit": per_user_limit,
        "expires_at": expires_at,
    }


@router.message(F.text.casefold().startswith("+промо"))
async def handle_promo_create(message: types.Message):
    """Создание промокода владельцем бота."""
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(
            "🎟 <b>Создание промокода</b>\n\n"
            "Формат: <code>+промо КОД МОНЕТЫ [РЕЙТИНГ] [ЛИМИТ] [НА_ЮЗЕРА]</code>\n\n"
            "Примеры:\n"
            "• <code>+промо SUMMER 1000</code> — 1000 монет, без ограничений\n"
            "• <code>+промо BOOST 5000 10 100 1</code> — 5000 монет, 10 рейтинга, 100 активаций, 1 на игрока\n"
            "• <code>+промо GIFT 500 срок 7д</code> — действует 7 дней\n\n"
            "<i>Лимит 0 = без ограничения. Доступно только владельцу.</i>",
            parse_mode="HTML",
        )
        return

    args = _parse_create_args(parts)
    async with session_scope() as session:
        try:
            promo = await create_promo(
                session,
                code=args["code"],
                coins=args["coins"],
                rating=args["rating"],
                max_activations=args["max_activations"],
                per_user_limit=args["per_user_limit"],
                created_by=message.from_user.id,
                expires_at=args["expires_at"],
            )
        except PromoError as e:
            await message.reply(f"❌ {e}")
            return

        limit_str = f"{promo.max_activations}" if promo.max_activations else "∞"
        exp_str = promo.expires_at.strftime("%d.%m.%Y %H:%M") if promo.expires_at else "нет"
        await message.reply(
            f"✅ <b>Промокод создан!</b>\n\n"
            f"🎟 Код: <code>{promo.code}</code>\n"
            f"💰 Монеты: <b>{promo.coins:,}</b>\n"
            f"⭐ Рейтинг: <b>{promo.rating}</b>\n"
            f"📊 Лимит активаций: <b>{limit_str}</b>\n"
            f"👤 На игрока: <b>{promo.per_user_limit}</b>\n"
            f"⏳ Действует до: <b>{exp_str}</b>".replace(",", " "),
            parse_mode="HTML",
        )


@router.message(F.text.casefold().in_(["промокоды", "!промокоды", "/promos", "промо список", "промо лист"]))
async def handle_promo_list(message: types.Message):
    """Список промокодов (только владелец)."""
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    async with session_scope() as session:
        promos = await list_promos(session, limit=30)
        if not promos:
            await message.reply("🎟 Промокодов пока нет. Создайте: <code>+промо КОД МОНЕТЫ</code>", parse_mode="HTML")
            return
        lines = ["🎟 <b>Промокоды Fable:</b>\n"]
        for p in promos:
            status = "🟢" if p.is_active else "🔴"
            limit_str = f"{p.activations}/{p.max_activations}" if p.max_activations else f"{p.activations}/∞"
            lines.append(
                f"{status} <code>{p.code}</code> — 💰{p.coins:,} ⭐{p.rating} | активаций: {limit_str}".replace(",", " ")
            )
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "промо выкл", "промо отключить", "промо вкл", "промо удалить"
])))
async def handle_promo_manage(message: types.Message):
    """Управление промокодом (только владелец): включить/выключить/удалить."""
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply("Использование: <code>промо выкл КОД</code>, <code>промо вкл КОД</code>, <code>промо удалить КОД</code>", parse_mode="HTML")
        return
    action = parts[1].casefold()
    code = parts[2]
    async with session_scope() as session:
        try:
            if action == "удалить":
                ok = await delete_promo(session, code=code)
                await message.reply("🗑 Промокод удалён." if ok else "❌ Промокод не найден.")
            else:
                active = action in ("вкл", "включить")
                promo = await set_promo_active(session, code=code, active=active)
                await message.reply(f"✅ Промокод <code>{promo.code}</code> {'включён' if active else 'выключен'}.", parse_mode="HTML")
        except PromoError as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().func(lambda t: any(t == p or t.startswith(p + " ") for p in [
    "промо", "!промо", "/promo"
])))
async def handle_promo_activate(message: types.Message):
    """Активация промокода игроком: промо КОД."""
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("🎟 Использование: <code>промо КОД</code>\n\nВведите промокод, чтобы получить награду.", parse_mode="HTML")
        return

    code = parts[1]
    if code.casefold() in ("список", "лист", "list"):
        if _is_owner(message.from_user.id):
            await handle_promo_list(message)
        return

    async with session_scope() as session:
        try:
            res = await activate_promo(session, code=code, user_id=message.from_user.id)
        except PromoError as e:
            await message.reply(f"❌ {e}")
            return

        reward_parts = []
        if res.coins:
            reward_parts.append(f"💰 <b>+{res.coins:,}</b> монет".replace(",", " "))
        if res.rating:
            reward_parts.append(f"⭐ <b>+{res.rating}</b> рейтинга")
        reward = ", ".join(reward_parts)
        await message.reply(
            f"🎉 <b>Промокод активирован!</b>\n\n"
            f"🎟 Код: <code>{res.promo.code}</code>\n"
            f"Награда: {reward}",
            parse_mode="HTML",
        )
