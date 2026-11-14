"""Глобальные настройки Fable Randomizer: ссылка поддержки, username бота,
включение/выключение отдельных уведомлений и список администраторов.

Значения хранятся в таблице `settings` (модель BotSetting) под ключом
"randomizer", поэтому сохраняются между перезапусками бота.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Admin, BotSetting

logger = logging.getLogger("fable.randomizer")

SETTINGS_KEY = "randomizer"

# Типы уведомлений и их отображение. Администратор может отключать каждое.
NOTIFICATION_TYPES: dict[str, str] = {
    "new_contest": "🎁 Новый конкурс",
    "contest_start": "🚀 Старт конкурса",
    "contest_ending": "⏰ Скоро окончание конкурса",
    "contest_finished": "🏁 Конкурс завершён",
    "winners_selected": "🏆 Выбраны победители",
    "registration_success": "✅ Успешная регистрация в конкурсе",
    "subscribe_required": "📢 Напоминание о подписке",
    "conditions_changed": "📝 Изменены условия конкурса",
    "contest_cancelled": "❌ Конкурс отменён",
}

DEFAULT_SETTINGS: dict = {
    "support_link": "",
    "bot_username": "",
    "notify_enabled": True,
    "notifications": {key: True for key in NOTIFICATION_TYPES},
    # Максимум накопленного реферального бонуса: 1000 юнитов = 10%.
    "max_referral_bonus_units": 1000,
}


async def get_settings_dict(session: AsyncSession) -> dict:
    """Возвращает настройки Randomizer, объединённые со значениями по умолчанию."""
    row = await session.get(BotSetting, SETTINGS_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(stored)
    # Гарантируем наличие всех ключей уведомлений (при добавлении новых типов).
    notif = dict(DEFAULT_SETTINGS["notifications"])
    notif.update(stored.get("notifications") or {})
    merged["notifications"] = notif
    return merged


async def save_settings_dict(session: AsyncSession, values: dict) -> dict:
    """Сохраняет переданные значения настроек Randomizer (частичное обновление)."""
    current = await get_settings_dict(session)
    current.update(values)
    row = await session.get(BotSetting, SETTINGS_KEY)
    if row is None:
        session.add(BotSetting(key=SETTINGS_KEY, value=current))
    else:
        row.value = current
    await session.flush()
    return current


async def get_bot_username(session: AsyncSession) -> str | None:
    """Возвращает сохранённый username бота (без @), если он известен."""
    cfg = await get_settings_dict(session)
    username = (cfg.get("bot_username") or "").strip().lstrip("@")
    return username or None


async def set_bot_username(session: AsyncSession, username: str) -> None:
    await save_settings_dict(session, {"bot_username": (username or "").lstrip("@")})


async def get_support_link(session: AsyncSession) -> str:
    cfg = await get_settings_dict(session)
    return (cfg.get("support_link") or "").strip()


async def set_support_link(session: AsyncSession, link: str) -> None:
    await save_settings_dict(session, {"support_link": (link or "").strip()})


async def is_notification_enabled(session: AsyncSession, event_type: str) -> bool:
    """Проверяет, разрешено ли отправлять уведомление данного типа."""
    cfg = await get_settings_dict(session)
    if not cfg.get("notify_enabled", True):
        return False
    return bool(cfg.get("notifications", {}).get(event_type, True))


async def set_notification_enabled(session: AsyncSession, event_type: str, enabled: bool) -> None:
    cfg = await get_settings_dict(session)
    notif = dict(cfg.get("notifications") or {})
    notif[event_type] = enabled
    await save_settings_dict(session, {"notifications": notif})


async def get_max_referral_bonus_units(session: AsyncSession) -> int:
    cfg = await get_settings_dict(session)
    try:
        return int(cfg.get("max_referral_bonus_units", 1000))
    except (TypeError, ValueError):
        return 1000


# ===================== АДМИНИСТРАТОРЫ =====================

async def get_admin_ids(session: AsyncSession) -> list[int]:
    """Возвращает список ID активных администраторов Randomizer (включая владельца)."""
    ids = {get_settings().owner_id}
    res = await session.scalars(select(Admin.user_id).where(Admin.is_active.is_(True)))
    ids.update(int(x) for x in res.all())
    return sorted(ids)


async def is_admin(session: AsyncSession, user_id: int) -> bool:
    if user_id == get_settings().owner_id:
        return True
    row = await session.scalar(
        select(Admin).where(Admin.user_id == user_id, Admin.is_active.is_(True))
    )
    return row is not None


async def add_admin(session: AsyncSession, user_id: int, *, added_by: int | None = None, level: int = 1) -> Admin:
    admin = await session.scalar(select(Admin).where(Admin.user_id == user_id))
    if admin is None:
        admin = Admin(user_id=user_id, level=level, is_active=True, added_by=added_by)
        session.add(admin)
    else:
        admin.is_active = True
        admin.level = level
    await session.flush()
    return admin


async def remove_admin(session: AsyncSession, user_id: int) -> bool:
    admin = await session.scalar(select(Admin).where(Admin.user_id == user_id))
    if not admin:
        return False
    admin.is_active = False
    return True
