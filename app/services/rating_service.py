"""Сервис рейтинга персонажа для Fable.

Правила:
- Рейтинг (авторитет/влияние): можно купить за монеты или продать обратно в монеты.
- Стоимость покупки: 100 монет за 1 единицу рейтинга.
- Стоимость продажи: 70 монет за 1 единицу рейтинга (комиссия 30%).
- Рейтинг также может выпадать из кейсов.
- Рейтинг отображается в профиле персонажа.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError


class RatingError(Exception):
    """Базовая ошибка операций с рейтингом."""


BUY_RATING_PRICE = 100   # Монет за 1 ед. рейтинга
SELL_RATING_PRICE = 70   # Монет за 1 ед. рейтинга при сдаче


async def buy_rating(session: AsyncSession, *, user_id: int, amount: int) -> tuple[int, int]:
    """Покупка рейтинга игроком. Возвращает (новый_рейтинг, потраченные_монеты)."""
    if amount <= 0:
        raise RatingError("Количество рейтинга должно быть больше нуля")

    cost = amount * BUY_RATING_PRICE
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cost:,} монет, у вас {player.wallet:,}".replace(",", " "))

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Покупка {amount} очков рейтинга",
        operation_type="rating_buy",
    )

    player.rating += amount
    await session.flush()
    return player.rating, cost


async def sell_rating(session: AsyncSession, *, user_id: int, amount: int) -> tuple[int, int]:
    """Продажа рейтинга игроком. Возвращает (остаток_рейтинга, полученные_монеты)."""
    if amount <= 0:
        raise RatingError("Количество рейтинга должно быть больше нуля")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.rating < amount:
        raise RatingError(f"Недостаточно рейтинга: у вас {player.rating:,}, вы хотите продать {amount:,}".replace(",", " "))

    payout = amount * SELL_RATING_PRICE
    player.rating -= amount

    await credit_coins(
        session,
        user_id=user_id,
        amount=payout,
        reason=f"Продажа {amount} очков рейтинга",
        operation_type="rating_sell",
    )

    await session.flush()
    return player.rating, payout
