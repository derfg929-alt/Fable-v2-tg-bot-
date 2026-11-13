"""Сервис промокодов Fable.

Промокоды создаются исключительно владельцем бота (в ЛС) и активируются
игроками командой "промо <код>". Промокод может выдавать монеты и очки рейтинга.
Поддерживаются лимит на количество активаций, лимит на одного игрока и срок действия.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PromoCode, PromoCodeActivation
from app.services.economy_service import credit_coins
from app.services.player_service import get_or_create_player


class PromoError(Exception):
    """Базовая ошибка промокодов."""


class PromoNotFoundError(PromoError):
    """Промокод не найден."""


class PromoInactiveError(PromoError):
    """Промокод отключён или истёк."""


class PromoExhaustedError(PromoError):
    """Исчерпан лимит активаций промокода."""


class PromoAlreadyUsedError(PromoError):
    """Игрок уже активировал этот промокод."""


_CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{2,64}$")


def normalize_code(code: str) -> str:
    """Приводит код к каноническому виду: верхний регистр без пробелов."""
    return (code or "").strip().upper()


@dataclass(frozen=True, slots=True)
class PromoCreateResult:
    promo: PromoCode


@dataclass(frozen=True, slots=True)
class PromoActivateResult:
    promo: PromoCode
    coins: int
    rating: int


async def create_promo(
    session: AsyncSession,
    *,
    code: str,
    coins: int = 0,
    rating: int = 0,
    max_activations: int = 0,
    per_user_limit: int = 1,
    created_by: int | None = None,
    description: str | None = None,
    expires_at: datetime | None = None,
) -> PromoCode:
    """Создаёт или перезаписывает промокод. Доступно только владельцу (проверяется в handler)."""
    norm = normalize_code(code)
    if not _CODE_RE.match(norm):
        raise PromoError("Код должен состоять из латинских букв, цифр, «-» или «_» (2–64 символа)")
    if coins < 0 or rating < 0:
        raise PromoError("Награда не может быть отрицательной")
    if coins == 0 and rating == 0:
        raise PromoError("Укажите награду: монеты и/или рейтинг")
    if max_activations < 0 or per_user_limit < 1:
        raise PromoError("Некорректные лимиты активаций")

    existing = await session.scalar(select(PromoCode).where(PromoCode.code == norm).with_for_update())
    if existing:
        existing.coins = coins
        existing.rating = rating
        existing.max_activations = max_activations
        existing.per_user_limit = per_user_limit
        existing.is_active = True
        existing.description = description
        existing.expires_at = expires_at
        await session.flush()
        return existing

    promo = PromoCode(
        code=norm,
        coins=coins,
        rating=rating,
        max_activations=max_activations,
        per_user_limit=per_user_limit,
        activations=0,
        is_active=True,
        expires_at=expires_at,
        created_by=created_by,
        description=description,
    )
    session.add(promo)
    await session.flush()
    return promo


async def set_promo_active(session: AsyncSession, *, code: str, active: bool) -> PromoCode:
    """Включает/выключает промокод."""
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == normalize_code(code)))
    if not promo:
        raise PromoNotFoundError(f"Промокод «{normalize_code(code)}» не найден")
    promo.is_active = active
    await session.flush()
    return promo


async def delete_promo(session: AsyncSession, *, code: str) -> bool:
    """Удаляет промокод вместе с историей активаций."""
    norm = normalize_code(code)
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == norm))
    if not promo:
        return False
    from sqlalchemy import delete

    await session.execute(delete(PromoCodeActivation).where(PromoCodeActivation.promo_id == promo.id))
    await session.delete(promo)
    await session.flush()
    return True


async def list_promos(session: AsyncSession, *, limit: int = 50) -> list[PromoCode]:
    """Возвращает список промокодов (свежие сверху)."""
    res = await session.scalars(
        select(PromoCode).order_by(PromoCode.created_at.desc()).limit(limit)
    )
    return list(res.all())


async def get_promo(session: AsyncSession, *, code: str) -> PromoCode | None:
    return await session.scalar(select(PromoCode).where(PromoCode.code == normalize_code(code)))


async def activate_promo(session: AsyncSession, *, code: str, user_id: int) -> PromoActivateResult:
    """Активирует промокод для игрока с полной проверкой лимитов."""
    norm = normalize_code(code)
    if not norm:
        raise PromoError("Укажите код: промо <код>")

    promo = await session.scalar(select(PromoCode).where(PromoCode.code == norm).with_for_update())
    if not promo:
        raise PromoNotFoundError(f"Промокод «{norm}» не найден")

    now = datetime.now(timezone.utc)
    if not promo.is_active:
        raise PromoInactiveError(f"Промокод «{norm}» отключён")
    if promo.expires_at is not None:
        expires = promo.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now > expires:
            raise PromoInactiveError(f"Срок действия промокода «{norm}» истёк")
    if promo.max_activations and promo.activations >= promo.max_activations:
        raise PromoExhaustedError(f"Лимит активаций промокода «{norm}» исчерпан")

    used = await session.scalar(
        select(func.count(PromoCodeActivation.id)).where(
            PromoCodeActivation.promo_id == promo.id,
            PromoCodeActivation.user_id == user_id,
        )
    ) or 0
    if used >= promo.per_user_limit:
        raise PromoAlreadyUsedError(f"Вы уже активировали промокод «{norm}»")

    await get_or_create_player(session, telegram_id=user_id, first_name="Игрок")

    if promo.coins > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=promo.coins,
            reason=f"Активация промокода {norm}",
            operation_type="reward",
            idempotency_key=f"promo:{norm}:{user_id}:{used}",
        )
    if promo.rating > 0:
        player = await session.get(Player, user_id)
        if player:
            player.rating += promo.rating

    session.add(PromoCodeActivation(promo_id=promo.id, code=norm, user_id=user_id))
    promo.activations += 1
    await session.flush()
    return PromoActivateResult(promo=promo, coins=promo.coins, rating=promo.rating)
