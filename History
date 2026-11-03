"""Read-only access to the global economy ledger.

Players may inspect transactions involving their own account. Only the configured
bot owner may inspect another player's history.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import EconomyTransaction


class HistoryAccessDenied(PermissionError):
    """Raised when a viewer requests another player's private history."""


@dataclass(frozen=True, slots=True)
class EconomyHistoryPage:
    items: Sequence[EconomyTransaction]
    has_more: bool
    target_user_id: int


async def get_economy_history(
    session: AsyncSession,
    *,
    viewer_id: int,
    target_user_id: int | None = None,
    operation_type: str | None = None,
    limit: int = 20,
    before_created_at: datetime | None = None,
    before_id: int | None = None,
) -> EconomyHistoryPage:
    """Return newest ledger entries for a player with owner-only cross-user access.

    Pagination uses the stable (created_at, id) descending cursor. Pass both
    cursor values from the last item of the previous page.
    """
    if viewer_id <= 0:
        raise ValueError("viewer_id должен быть положительным Telegram ID")

    target = viewer_id if target_user_id is None else target_user_id
    if target <= 0:
        raise ValueError("target_user_id должен быть положительным Telegram ID")

    owner_id = get_settings().owner_id
    if target != viewer_id and viewer_id != owner_id:
        raise HistoryAccessDenied("Можно просматривать только собственную историю")

    if not 1 <= limit <= 100:
        raise ValueError("limit должен быть от 1 до 100")
    if (before_created_at is None) != (before_id is None):
        raise ValueError("Для пагинации передайте одновременно before_created_at и before_id")

    conditions = [or_(
        EconomyTransaction.actor_id == target,
        EconomyTransaction.recipient_id == target,
    )]
    if operation_type:
        normalized_type = operation_type.strip().lower()
        if not normalized_type:
            raise ValueError("operation_type не может быть пустым")
        conditions.append(EconomyTransaction.operation_type == normalized_type)

    if before_created_at is not None and before_id is not None:
        conditions.append(or_(
            EconomyTransaction.created_at < before_created_at,
            and_(
                EconomyTransaction.created_at == before_created_at,
                EconomyTransaction.id < before_id,
            ),
        ))

    stmt = (
        select(EconomyTransaction)
        .where(*conditions)
        .order_by(EconomyTransaction.created_at.desc(), EconomyTransaction.id.desc())
        .limit(limit + 1)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    return EconomyHistoryPage(
        items=tuple(rows[:limit]),
        has_more=has_more,
        target_user_id=target,
    )


async def get_economy_transaction(
    session: AsyncSession,
    *,
    viewer_id: int,
    transaction_id: int,
) -> EconomyTransaction | None:
    """Fetch a transaction only if it involves the viewer, or viewer is owner."""
    if viewer_id <= 0 or transaction_id <= 0:
        raise ValueError("ID должны быть положительными")
    stmt = select(EconomyTransaction).where(EconomyTransaction.id == transaction_id)
    result = await session.execute(stmt)
    transaction = result.scalar_one_or_none()
    if transaction is None:
        return None

    if viewer_id != get_settings().owner_id and viewer_id not in (
        transaction.actor_id,
        transaction.recipient_id,
    ):
        raise HistoryAccessDenied("Эта операция не относится к вашему аккаунту")
    return transaction
