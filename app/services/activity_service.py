"""Награды за активность в чатах без автоматических санкций.

Баланс и опыт глобальные; лимиты и кулдаун учитываются отдельно по каждому чату.
Повторная обработка одного Telegram message_id безопасна благодаря
уникальному idempotency_key в журнале экономики.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRecord, EconomyTransaction, GlobalSetting, Player
from app.services.economy_service import credit_coins


@dataclass(frozen=True, slots=True)
class ActivityRewardResult:
    awarded: bool
    reason: str
    coins: int = 0
    experience: int = 0
    level: int | None = None


DEFAULTS = {
    "enabled": True,
    "coins_per_message": 2,
    "experience_per_message": 1,
    "cooldown_seconds": 60,
    "daily_message_limit": 40,
    "minimum_length": 12,
    "duplicate_text_window_minutes": 10,
    "daily_coin_limit": 100,
    "experience_per_level": 100,
}


def _text_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def get_activity_settings(session: AsyncSession) -> dict:
    row = await session.get(GlobalSetting, "activity_rewards")
    settings = {**DEFAULTS, **(row.value if row else {})}
    for key in (
        "coins_per_message", "experience_per_message", "cooldown_seconds",
        "daily_message_limit", "minimum_length",
        "duplicate_text_window_minutes", "daily_coin_limit",
        "experience_per_level",
    ):
        if not isinstance(settings[key], int) or settings[key] < 0:
            raise ValueError(f"Некорректная настройка активности: {key}")
    return settings


async def set_activity_settings(
    session: AsyncSession, *, values: dict
) -> dict:
    """Обновляет настройки; вызывать только после проверки владельца в handler."""
    unknown = set(values) - (set(DEFAULTS) - {"enabled"})
    if "enabled" in values and not isinstance(values["enabled"], bool):
        raise ValueError("enabled должен быть bool")
    if unknown:
        raise ValueError("Неизвестные настройки: " + ", ".join(sorted(unknown)))
    merged = {**await get_activity_settings(session), **values}
    for key in set(values) - {"enabled"}:
        if not isinstance(values[key], int) or isinstance(values[key], bool) or values[key] < 0:
            raise ValueError(f"{key} должен быть неотрицательным целым числом")
    stmt = insert(GlobalSetting).values(key="activity_rewards", value=merged)
    stmt = stmt.on_conflict_do_update(
        index_elements=[GlobalSetting.key], set_={"value": merged}
    )
    await session.execute(stmt)
    return merged


async def reward_chat_message(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    text: str,
    is_bot: bool = False,
    now: datetime | None = None,
) -> ActivityRewardResult:
    """Проверяет требования и выдаёт монеты/опыт за одно сообщение участника."""
    if chat_id == 0 or user_id <= 0 or message_id <= 0:
        raise ValueError("Некорректные идентификаторы сообщения")
    if is_bot:
        return ActivityRewardResult(False, "bot_message")
    body = (text or "").strip()
    settings = await get_activity_settings(session)
    if not settings["enabled"]:
        return ActivityRewardResult(False, "disabled")
    if len(body) < settings["minimum_length"]:
        return ActivityRewardResult(False, "message_too_short")
    if settings["coins_per_message"] == 0 and settings["experience_per_message"] == 0:
        return ActivityRewardResult(False, "no_reward_configured")

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("now должен содержать часовой пояс")
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    key = f"activity:{chat_id}:{message_id}"
    fingerprint = _text_fingerprint(body)

    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id).with_for_update()
    )
    if player is None:
        return ActivityRewardResult(False, "player_not_registered")

    duplicate = await session.scalar(
        select(EconomyTransaction.id).where(
            EconomyTransaction.idempotency_key == key
        )
    )
    if duplicate is not None:
        return ActivityRewardResult(False, "already_processed")

    last_activity = await session.scalar(
        select(func.max(ActivityRecord.created_at)).where(
            ActivityRecord.chat_id == chat_id,
            ActivityRecord.user_id == user_id,
        )
    )
    if last_activity is not None and moment - last_activity < timedelta(
        seconds=settings["cooldown_seconds"]
    ):
        return ActivityRewardResult(False, "cooldown")

    today_count = await session.scalar(
        select(func.count(ActivityRecord.id)).where(
            ActivityRecord.chat_id == chat_id,
            ActivityRecord.user_id == user_id,
            ActivityRecord.created_at >= day_start,
        )
    )
    if (today_count or 0) >= settings["daily_message_limit"]:
        return ActivityRewardResult(False, "daily_message_limit")

    repeat_since = moment - timedelta(
        minutes=settings["duplicate_text_window_minutes"]
    )
    recent = await session.execute(
        select(EconomyTransaction.metadata_json).where(
            EconomyTransaction.operation_type == "activity_reward",
            EconomyTransaction.recipient_id == user_id,
            EconomyTransaction.created_at >= repeat_since,
        ).limit(100)
    )
    if any((row or {}).get("text_hash") == fingerprint for row in recent.scalars()):
        return ActivityRewardResult(False, "repeated_text")

    coins = settings["coins_per_message"]
    if settings["daily_coin_limit"]:
        earned_today = await session.scalar(
            select(func.coalesce(func.sum(ActivityRecord.reward_coins), 0)).where(
                ActivityRecord.chat_id == chat_id,
                ActivityRecord.user_id == user_id,
                ActivityRecord.created_at >= day_start,
            )
        )
        coins = min(coins, max(0, settings["daily_coin_limit"] - int(earned_today or 0)))
    xp = settings["experience_per_message"]
    if coins <= 0 and xp <= 0:
        return ActivityRewardResult(False, "daily_reward_limit")

    if coins:
        await credit_coins(
            session,
            user_id=user_id,
            amount=coins,
            reason=f"Активность в чате {chat_id}",
            actor_id=None,
            operation_type="activity_reward",
            reference=f"{chat_id}:{message_id}",
            idempotency_key=key,
            metadata={"chat_id": chat_id, "message_id": message_id, "text_hash": fingerprint},
        )
    else:
        # Even an XP-only reward must reserve the message idempotency key.
        stmt = insert(EconomyTransaction).values(
            operation_type="activity_reward", amount=0, actor_id=None,
            recipient_id=user_id, actor_balance_delta=0,
            recipient_balance_delta=0, reason="Опыт за активность",
            reference=f"{chat_id}:{message_id}", idempotency_key=key,
            metadata_json={"chat_id": chat_id, "message_id": message_id, "text_hash": fingerprint},
        ).on_conflict_do_nothing(index_elements=[EconomyTransaction.idempotency_key])
        inserted = (await session.execute(stmt.returning(EconomyTransaction.id))).scalar_one_or_none()
        if inserted is None:
            return ActivityRewardResult(False, "already_processed")

    player.experience += xp
    threshold = settings["experience_per_level"]
    levels_gained = 0
    if threshold > 0:
        while player.experience >= threshold:
            player.experience -= threshold
            player.level += 1
            levels_gained += 1
    session.add(ActivityRecord(
        chat_id=chat_id, user_id=user_id, points=1,
        message_length=len(body), reward_coins=coins,
    ))
    return ActivityRewardResult(
        True, "awarded", coins=coins, experience=xp, level=player.level if levels_gained else None
    )

# Алиас для обработчиков
process_message_activity = reward_chat_message
