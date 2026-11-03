"""Глобальная репутация игроков Fable."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import GlobalSetting, Player, ReputationAction


DEFAULTS = {
    "cooldown_seconds": 86400,
    "daily_limit": 1,
    "minimum_reputation": 0,
    "maximum_reputation": 1000000,
}


class ReputationError(ValueError):
    """Ошибка бизнес-правил изменения репутации."""


@dataclass(frozen=True, slots=True)
class ReputationResult:
    actor_id: int
    target_id: int
    delta: int
    reputation: int
    changed_at: datetime


async def get_reputation_settings(session: AsyncSession) -> dict:
    row = await session.get(GlobalSetting, "reputation")
    values = {**DEFAULTS, **(row.value if row else {})}
    for key in DEFAULTS:
        if not isinstance(values[key], int) or isinstance(values[key], bool) or values[key] < 0:
            raise ReputationError(f"Некорректная настройка {key}")
    if values["maximum_reputation"] < values["minimum_reputation"]:
        raise ReputationError("maximum_reputation меньше minimum_reputation")
    return values


async def set_reputation_settings(session: AsyncSession, *, values: dict) -> dict:
    """Сохраняет параметры; вызывающий обработчик обязан проверить OWNER_ID."""
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ReputationError("Неизвестные параметры: " + ", ".join(sorted(unknown)))
    for key, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReputationError(f"{key} должен быть неотрицательным целым числом")
    merged = {**await get_reputation_settings(session), **values}
    if merged["maximum_reputation"] < merged["minimum_reputation"]:
        raise ReputationError("maximum_reputation меньше minimum_reputation")
    stmt = insert(GlobalSetting).values(key="reputation", value=merged)
    stmt = stmt.on_conflict_do_update(
        index_elements=[GlobalSetting.key], set_={"value": merged}
    )
    await session.execute(stmt)
    return merged


async def change_reputation(
    session: AsyncSession,
    *,
    actor_id: int,
    target_id: int,
    direction: int,
    chat_id: int | None = None,
    now: datetime | None = None,
) -> ReputationResult:
    """Выдать +1 или -1 репутации с глобальным кулдауном и дневным лимитом."""
    if actor_id <= 0 or target_id <= 0:
        raise ReputationError("Telegram ID должны быть положительными")
    if actor_id == target_id:
        raise ReputationError("Нельзя изменять собственную репутацию")
    if direction not in (-1, 1):
        raise ReputationError("direction должен быть +1 или -1")

    settings = await get_reputation_settings(session)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ReputationError("now должен содержать часовой пояс")
    local_now = moment.astimezone(ZoneInfo(get_settings().timezone))
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    # Одинаковый порядок блокировок предотвращает взаимную блокировку при
    # одновременных взаимных действиях двух игроков.
    players = (await session.execute(
        select(Player).where(Player.telegram_id.in_(sorted({actor_id, target_id})))
        .order_by(Player.telegram_id).with_for_update()
    )).scalars().all()
    by_id = {player.telegram_id: player for player in players}
    if actor_id not in by_id:
        raise ReputationError("Сначала зарегистрируйтесь в Fable")
    if target_id not in by_id:
        raise ReputationError("Игрок не зарегистрирован в Fable")

    last_action = await session.scalar(
        select(func.max(ReputationAction.created_at)).where(
            ReputationAction.actor_id == actor_id
        )
    )
    if last_action is not None and moment - last_action < timedelta(
        seconds=settings["cooldown_seconds"]
    ):
        raise ReputationError("Кулдаун изменения репутации ещё не закончился")

    used_today = await session.scalar(
        select(func.count(ReputationAction.id)).where(
            ReputationAction.actor_id == actor_id,
            ReputationAction.created_at >= day_start,
        )
    )
    if (used_today or 0) >= settings["daily_limit"]:
        raise ReputationError("Дневной лимит изменения репутации исчерпан")

    target = by_id[target_id]
    new_value = target.reputation + direction
    if new_value < settings["minimum_reputation"]:
        raise ReputationError("Нельзя опустить репутацию ниже установленного минимума")
    if new_value > settings["maximum_reputation"]:
        raise ReputationError("Достигнут максимум репутации")

    target.reputation = new_value
    action = ReputationAction(
        actor_id=actor_id,
        target_id=target_id,
        delta=direction,
        chat_id=chat_id,
    )
    session.add(action)
    await session.flush()
    return ReputationResult(
        actor_id=actor_id,
        target_id=target_id,
        delta=direction,
        reputation=new_value,
        changed_at=action.created_at or moment,
    )


async def get_reputation(session: AsyncSession, *, user_id: int) -> int:
    if user_id <= 0:
        raise ReputationError("Telegram ID должен быть положительным")
    player = await session.get(Player, user_id)
    if player is None:
        raise ReputationError("Игрок не зарегистрирован в Fable")
    return player.reputation
