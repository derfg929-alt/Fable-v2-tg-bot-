"""Сервис аудита действий администраторов Fable Randomizer.

Каждое значимое действие администратора (создание/изменение/удаление конкурса,
управление каналами, блокировки, розыгрыши, рассылки) записывается в таблицу
`audit_logs` и, при наличии, дублируется в технический лог.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog

logger = logging.getLogger("fable.audit")


async def log_action(
    session: AsyncSession,
    *,
    actor_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict | None = None,
) -> AuditLog:
    """Записывает действие администратора в аудит."""
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details or {},
    )
    session.add(entry)
    await session.flush()
    logger.info("Аудит: %s (актор %s, цель %s:%s)", action, actor_id, target_type, target_id)
    return entry


async def get_recent_actions(session: AsyncSession, *, limit: int = 20) -> list[AuditLog]:
    """Возвращает последние действия администраторов."""
    res = await session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    return list(res.all())
