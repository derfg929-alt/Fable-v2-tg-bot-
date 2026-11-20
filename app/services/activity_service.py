"""Награды за активность в чатах без автоматических санкций.

Баланс глобальный; лимиты и кулдаун учитываются отдельно по каждому чату.
Повторная обработка одного Telegram message_id безопасна благодаря
уникальному idempotency_key в журнале экономики.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRecord, Chat, EconomyTransaction, GlobalSetting, Player
from app.services.economy_service import credit_coins


@dataclass(frozen=True, slots=True)
class ActivityRewardResult:
    awarded: bool
    reason: str
    coins: int = 0


DEFAULTS = {
    "enabled": True,
    # Максимальная награда за одно сообщение; фактическая награда — случайная 1..N.
    "coins_per_message": 50,
    "cooldown_seconds": 60,
    "daily_message_limit": 40,
    "minimum_length": 12,
    "duplicate_text_window_minutes": 10,
    # Дневной потолок монет за активность: держит монеты ценными.
    "daily_coin_limit": 100,
}


def _text_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def get_activity_settings(session: AsyncSession) -> dict:
    row = await session.get(GlobalSetting, "activity_rewards")
    settings = {**DEFAULTS, **(row.value if row else {})}
    for key in (
        "coins_per_message", "cooldown_seconds", "daily_message_limit",
        "minimum_length", "duplicate_text_window_minutes", "daily_coin_limit",
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
    """Проверяет требования и выдаёт монеты за одно сообщение участника."""
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
    if settings["coins_per_message"] == 0:
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
            ActivityRecord.reward_coins > 0,
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

    # Награда за сообщение — случайная величина 1..coins_per_message.
    coins = random.randint(1, settings["coins_per_message"]) if settings["coins_per_message"] > 0 else 0
    if settings["daily_coin_limit"]:
        earned_today = await session.scalar(
            select(func.coalesce(func.sum(ActivityRecord.reward_coins), 0)).where(
                ActivityRecord.chat_id == chat_id,
                ActivityRecord.user_id == user_id,
                ActivityRecord.created_at >= day_start,
            )
        )
        coins = min(coins, max(0, settings["daily_coin_limit"] - int(earned_today or 0)))
    if coins <= 0:
        return ActivityRewardResult(False, "daily_reward_limit")

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

    record = await session.scalar(
        select(ActivityRecord).where(
            ActivityRecord.chat_id == chat_id,
            ActivityRecord.user_id == user_id,
            ActivityRecord.message_id == message_id,
        )
    )
    if record is not None:
        record.reward_coins = coins
        record.message_length = len(body)
    else:
        session.add(ActivityRecord(
            chat_id=chat_id, user_id=user_id, points=1,
            message_length=len(body), reward_coins=coins, message_id=message_id,
        ))
    # Сессия использует autoflush=False, поэтому фиксируем запись активности явно:
    # иначе кулдаун и дневные лимиты не увидят её в рамках той же сессии.
    await session.flush()
    return ActivityRewardResult(True, "awarded", coins=coins)


async def record_message_activity(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
    text: str,
) -> bool:
    """Учитывает сообщение в статистике, даже если монеты за него не начислены.

    reward_chat_message платит только за часть сообщений (кулдаун, дневной
    лимит, минимальная длина), поэтому без этой записи профиль показывал лишь
    оплаченные сообщения — сильно меньше реальных.
    """
    if chat_id == 0 or user_id <= 0 or message_id <= 0:
        return False
    existing = await session.scalar(
        select(ActivityRecord.id).where(
            ActivityRecord.chat_id == chat_id,
            ActivityRecord.user_id == user_id,
            ActivityRecord.message_id == message_id,
        )
    )
    if existing is not None:
        return False
    session.add(ActivityRecord(
        chat_id=chat_id,
        user_id=user_id,
        points=1,
        message_length=len((text or "").strip()),
        reward_coins=0,
        message_id=message_id,
    ))
    await session.flush()
    return True


async def get_chat_activity_breakdown(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    now: datetime | None = None,
) -> dict:
    """Возвращает активность игрока в конкретном чате:
    день, неделя, месяц, год, все время, а также время последней активности.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    # 1. Последняя активность
    last_active = await session.scalar(
        select(func.max(ActivityRecord.created_at)).where(
            ActivityRecord.chat_id == chat_id,
            ActivityRecord.user_id == user_id,
        )
    )

    # 2. Активность за день (24 часа)
    day_since = moment - timedelta(days=1)
    day_count = (
        await session.scalar(
            select(func.count(ActivityRecord.id)).where(
                ActivityRecord.chat_id == chat_id,
                ActivityRecord.user_id == user_id,
                ActivityRecord.created_at >= day_since,
            )
        )
        or 0
    )

    # 3. Активность за неделю (7 дней)
    week_since = moment - timedelta(days=7)
    week_count = (
        await session.scalar(
            select(func.count(ActivityRecord.id)).where(
                ActivityRecord.chat_id == chat_id,
                ActivityRecord.user_id == user_id,
                ActivityRecord.created_at >= week_since,
            )
        )
        or 0
    )

    # 4. Активность за месяц (30 дней)
    month_since = moment - timedelta(days=30)
    month_count = (
        await session.scalar(
            select(func.count(ActivityRecord.id)).where(
                ActivityRecord.chat_id == chat_id,
                ActivityRecord.user_id == user_id,
                ActivityRecord.created_at >= month_since,
            )
        )
        or 0
    )

    # 5. Активность за год (365 дней)
    year_since = moment - timedelta(days=365)
    year_count = (
        await session.scalar(
            select(func.count(ActivityRecord.id)).where(
                ActivityRecord.chat_id == chat_id,
                ActivityRecord.user_id == user_id,
                ActivityRecord.created_at >= year_since,
            )
        )
        or 0
    )

    # 6. За все время
    all_time_count = (
        await session.scalar(
            select(func.count(ActivityRecord.id)).where(
                ActivityRecord.chat_id == chat_id,
                ActivityRecord.user_id == user_id,
            )
        )
        or 0
    )

    return {
        "day": day_count,
        "week": week_count,
        "month": month_count,
        "year": year_count,
        "all_time": all_time_count,
        "last_active": last_active,
    }


async def get_global_activity_breakdown(
    session: AsyncSession,
    *,
    user_id: int,
    now: datetime | None = None,
) -> dict:
    """Активность игрока по всем чатам сразу.

    Возвращает те же окна, что и активность в чате, плюс число чатов и топ
    чатов по количеству сообщений — чтобы профиль показывал, сколько всего
    человек написал в боте, а не только в текущей группе.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    async def _count(since: datetime | None) -> int:
        stmt = select(func.count(ActivityRecord.id)).where(ActivityRecord.user_id == user_id)
        if since is not None:
            stmt = stmt.where(ActivityRecord.created_at >= since)
        return (await session.scalar(stmt)) or 0

    last_active = await session.scalar(
        select(func.max(ActivityRecord.created_at)).where(ActivityRecord.user_id == user_id)
    )

    chats_count = (
        await session.scalar(
            select(func.count(func.distinct(ActivityRecord.chat_id))).where(
                ActivityRecord.user_id == user_id
            )
        )
    ) or 0

    top_rows = (
        await session.execute(
            select(
                ActivityRecord.chat_id,
                Chat.title,
                func.count(ActivityRecord.id).label("messages"),
            )
            .join(Chat, Chat.telegram_id == ActivityRecord.chat_id, isouter=True)
            .where(ActivityRecord.user_id == user_id)
            .group_by(ActivityRecord.chat_id, Chat.title)
            .order_by(func.count(ActivityRecord.id).desc())
            .limit(5)
        )
    ).all()

    return {
        "day": await _count(moment - timedelta(days=1)),
        "week": await _count(moment - timedelta(days=7)),
        "month": await _count(moment - timedelta(days=30)),
        "year": await _count(moment - timedelta(days=365)),
        "all_time": await _count(None),
        "last_active": last_active,
        "chats": chats_count,
        "top_chats": [
            {"chat_id": row[0], "title": row[1] or f"Чат {row[0]}", "messages": row[2]}
            for row in top_rows
        ],
    }


# Алиас для обработчиков
process_message_activity = reward_chat_message
