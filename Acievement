"""Каталог и выдача достижений Fable.

Условия получения достижений проверяются игровыми сценариями отдельно:
этот сервис отвечает за каталог, безопасную выдачу и награду монетами.
Все операции выполняются в транзакции вызывающего кода; commit здесь нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Achievement, PlayerAchievement
from app.services.economy_service import EconomyResult, credit_coins


class AchievementError(Exception):
    """Базовая ошибка достижений."""


class AchievementNotFoundError(AchievementError):
    """Достижение с указанным кодом отсутствует в каталоге."""


class InvalidAchievementError(AchievementError):
    """Некорректное описание достижения."""


@dataclass(frozen=True, slots=True)
class AchievementDefinition:
    code: str
    title: str
    description: str = ""
    reward_coins: int = 0


@dataclass(frozen=True, slots=True)
class AchievementAward:
    achievement: Achievement
    player_achievement: PlayerAchievement | None
    economy_result: EconomyResult | None
    already_unlocked: bool

    @property
    def reward_coins(self) -> int:
        return self.achievement.reward_coins if not self.already_unlocked else 0


def _validate_definition(item: AchievementDefinition) -> None:
    if not item.code or len(item.code) > 64 or item.code.strip() != item.code:
        raise InvalidAchievementError("Код достижения должен содержать 1–64 символа без пробелов по краям")
    if not item.title or len(item.title) > 128:
        raise InvalidAchievementError("Название достижения должно содержать 1–128 символов")
    if len(item.description) > 10000:
        raise InvalidAchievementError("Описание достижения слишком длинное")
    if isinstance(item.reward_coins, bool) or not isinstance(item.reward_coins, int) or item.reward_coins < 0:
        raise InvalidAchievementError("Награда должна быть целым неотрицательным числом монет")


async def upsert_achievement_catalog(
    session: AsyncSession,
    definitions: Iterable[AchievementDefinition],
) -> int:
    """Добавляет/обновляет каталог; возвращает число обработанных описаний."""
    count = 0
    for item in definitions:
        _validate_definition(item)
        statement = insert(Achievement).values(
            code=item.code,
            title=item.title,
            description=item.description,
            reward_coins=item.reward_coins,
        ).on_conflict_do_update(
            index_elements=[Achievement.code],
            set_={
                "title": item.title,
                "description": item.description,
                "reward_coins": item.reward_coins,
            },
        )
        await session.execute(statement)
        count += 1
    return count


async def get_achievement(
    session: AsyncSession, code: str
) -> Achievement | None:
    return await session.scalar(
        select(Achievement).where(Achievement.code == code)
    )


async def list_achievements(
    session: AsyncSession,
    *,
    user_id: int | None = None,
) -> list[tuple[Achievement, bool]]:
    """Возвращает каталог и признак открытия игроком (если user_id задан)."""
    if user_id is None:
        rows = await session.scalars(
            select(Achievement).order_by(Achievement.id)
        )
        return [(item, False) for item in rows.all()]

    statement = (
        select(Achievement, PlayerAchievement.id)
        .outerjoin(
            PlayerAchievement,
            (PlayerAchievement.achievement_id == Achievement.id)
            & (PlayerAchievement.user_id == user_id),
        )
        .order_by(Achievement.id)
    )
    rows = (await session.execute(statement)).all()
    return [(achievement, award_id is not None) for achievement, award_id in rows]


async def get_player_achievements(
    session: AsyncSession,
    user_id: int,
) -> list[tuple[Achievement, PlayerAchievement]]:
    statement = (
        select(Achievement, PlayerAchievement)
        .join(PlayerAchievement, PlayerAchievement.achievement_id == Achievement.id)
        .where(PlayerAchievement.user_id == user_id)
        .order_by(PlayerAchievement.unlocked_at.desc(), Achievement.id)
    )
    return list((await session.execute(statement)).all())


async def award_achievement(
    session: AsyncSession,
    *,
    user_id: int,
    code: str,
) -> AchievementAward:
    """Выдаёт достижение ровно один раз и начисляет монеты ровно один раз.

    Уникальность (user_id, achievement_id) защищает от параллельных повторов.
    Ключ награды детерминированный и дополнительно защищает журнал экономики.
    """
    if user_id <= 0:
        raise ValueError("Telegram ID должен быть положительным")

    achievement = await get_achievement(session, code)
    if achievement is None:
        raise AchievementNotFoundError(f"Достижение «{code}» не найдено")

    statement = (
        insert(PlayerAchievement)
        .values(user_id=user_id, achievement_id=achievement.id)
        .on_conflict_do_nothing(
            index_elements=[
                PlayerAchievement.user_id,
                PlayerAchievement.achievement_id,
            ]
        )
        .returning(PlayerAchievement.id)
    )
    award_id = (await session.execute(statement)).scalar_one_or_none()

    if award_id is None:
        existing = await session.scalar(
            select(PlayerAchievement).where(
                PlayerAchievement.user_id == user_id,
                PlayerAchievement.achievement_id == achievement.id,
            )
        )
        return AchievementAward(
            achievement=achievement,
            player_achievement=existing,
            economy_result=None,
            already_unlocked=True,
        )

    player_achievement = await session.get(PlayerAchievement, award_id)
    if player_achievement is None:
        raise RuntimeError("Выдача достижения создана, но не найдена")

    economy_result = None
    if achievement.reward_coins > 0:
        economy_result = await credit_coins(
            session,
            user_id=user_id,
            amount=achievement.reward_coins,
            reason=f"Награда за достижение: {achievement.title}",
            operation_type="reward",
            reference=f"achievement:{achievement.code}",
            idempotency_key=f"achievement:{user_id}:{achievement.code}",
            metadata={"achievement_code": achievement.code},
        )

    return AchievementAward(
        achievement=achievement,
        player_achievement=player_achievement,
        economy_result=economy_result,
        already_unlocked=False,
    )
