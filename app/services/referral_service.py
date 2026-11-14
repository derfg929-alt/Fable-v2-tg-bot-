"""Реферальная система Fable Randomizer.

Правила из ТЗ:
- Каждый пользователь получает персональную ссылку вида
  https://t.me/BOT_USERNAME?start=ref_<ID>.
- За каждого нового пользователя пригласившему начисляется внутренний бонус
  0.01% дополнительного веса при выборе победителя (1 "юнит").
- Максимальный накопленный бонус — 10% (1000 юнитов).
- Запрещены саморефералы, повторное начисление и смена пригласившего.
- Бонус влияет только на конкурсы, в которых пользователь действительно участвует.
- Конкретные проценты пользователю не показываются.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, Referral
from app.services.player_service import get_player
from app.services.randomizer_settings import get_max_referral_bonus_units

logger = logging.getLogger("fable.referral")

# Один приглашённый = 1 юнит = 0.01% дополнительного веса.
BONUS_UNITS_PER_REFERRAL = 1
DEFAULT_MAX_UNITS = 1000  # 10%


def build_referral_link(bot_username: str, user_id: int) -> str:
    """Формирует персональную реферальную ссылку."""
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def parse_referral_payload(payload: str | None) -> int | None:
    """Извлекает Telegram ID пригласившего из payload диплинка (/start ref_123)."""
    if not payload:
        return None
    payload = payload.strip()
    if payload.startswith("ref_"):
        payload = payload[4:]
    if payload.isdigit():
        return int(payload)
    return None


async def count_invited(session: AsyncSession, inviter_id: int) -> int:
    """Количество приглашённых пользователей."""
    return int(
        await session.scalar(
            select(func.count(Referral.id)).where(Referral.inviter_id == inviter_id)
        )
        or 0
    )


async def get_referral_bonus_units(session: AsyncSession, inviter_id: int) -> int:
    """Накопленный бонус пригласившего в юнитах (1 юнит = 0.01%), с ограничением."""
    total = int(
        await session.scalar(
            select(func.coalesce(func.sum(Referral.bonus_units), 0)).where(
                Referral.inviter_id == inviter_id
            )
        )
        or 0
    )
    max_units = await get_max_referral_bonus_units(session)
    return min(total, max_units)


async def register_referral(
    session: AsyncSession,
    *,
    inviter_id: int,
    invited_id: int,
) -> bool:
    """Регистрирует факт приглашения. Возвращает True, если бонус был начислен.

    Защищает от самореферала, повторной регистрации и начисления бонуса
    уже существующему пользователю (вызывающий код передаёт invited_id только
    для нового пользователя).
    """
    if inviter_id == invited_id:
        return False
    if inviter_id <= 0 or invited_id <= 0:
        return False

    inviter = await get_player(session, inviter_id)
    if inviter is None:
        return False

    # Уже привязан к кому-то — повторное начисление запрещено.
    existing = await session.scalar(
        select(Referral).where(Referral.invited_id == invited_id)
    )
    if existing is not None:
        return False

    # Защита от гонок: уникальность invited_id обеспечивается схемой; при конфликте
    # просто не начисляем бонус повторно. Savepoint не рушит внешнюю транзакцию.
    try:
        async with session.begin_nested():
            session.add(
                Referral(
                    inviter_id=inviter_id,
                    invited_id=invited_id,
                    bonus_units=BONUS_UNITS_PER_REFERRAL,
                )
            )
            await session.flush()
    except Exception as e:  # noqa: BLE001 — уникальное ограничение invited_id
        logger.info("Реферал для %s уже существует (%s)", invited_id, e)
        return False

    # Фиксируем пригласившего в глобальном профиле (только если он не задан).
    invited = await get_player(session, invited_id)
    if invited is not None and not invited.referred_by:
        invited.referred_by = inviter_id

    logger.info("Реферал: игрок %s приглашён игроком %s", invited_id, inviter_id)
    return True


async def list_invited(
    session: AsyncSession, inviter_id: int, *, limit: int = 50
) -> list[tuple[Referral, Player | None]]:
    """Список приглашённых с датами и статусом регистрации."""
    refs = (
        await session.scalars(
            select(Referral)
            .where(Referral.inviter_id == inviter_id)
            .order_by(Referral.invited_at.desc())
            .limit(limit)
        )
    ).all()
    result: list[tuple[Referral, Player | None]] = []
    for ref in refs:
        player = await get_player(session, ref.invited_id)
        result.append((ref, player))
    return result
