"""Сервис владельца бота (Telegram ID 8039111975) Fable.

Правила из ТЗ:
- Доступ только владельцу в личных сообщениях.
- 13.1. Управление чатами:
  * "настройки чаты" — список групп бота.
  * "настройки чат ID" — настройки конкретного чата.
  * "настройки стоп ID", "настройки старт ID" — остановка/возобновление работы бота в конкретном чате.
- 13.2-13.4. Настройки экономики, игр, биржи:
  * Сохранение глобальных параметров в GlobalSetting.
- 13.5. Глобальный бан игрока:
  * Запрет пользоваться экономикой, биржей, играми.
  * Не должен автоматически выдавать Telegram-бан во всех группах.
  * Снятие глобального бана.
- 13.6. Глобальный сброс экономики:
  * Опасная операция, требует подтверждения.
  * Обнуляет балансы (wallet, bank, shares) без удаления аккаунтов и истории аудита.
  * Логируется в аудит.
- 13.7. Рассылка сообщений по активным группам.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging

from sqlalchemy import delete, desc, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    AdminActionLog,
    Chat,
    GlobalSetting,
    OwnerBroadcast,
    Player,
)

logger = logging.getLogger(__name__)

# Рассылка владельца разрешена не чаще одного раза в 24 часа.
BROADCAST_COOLDOWN_SECONDS = 24 * 60 * 60
_BROADCAST_STATE_KEY = "owner_broadcast_state"


class OwnerAccessDeniedError(PermissionError):
    """Доступ разрешен только владельцу бота."""


def verify_owner(user_id: int) -> None:
    settings = get_settings()
    if user_id != settings.owner_id:
        raise OwnerAccessDeniedError("Данная команда доступна только владельцу бота в ЛС.")


async def list_chats_summary(
    session: AsyncSession, *, owner_id: int
) -> list[Chat]:
    """Возвращает список подключенных к боту чатов."""
    verify_owner(owner_id)
    res = await session.scalars(select(Chat).order_by(Chat.connected_at.desc()))
    return list(res.all())


async def list_players_wealth_summary(
    session: AsyncSession, *, owner_id: int, limit: int = 25
) -> list[Player]:
    """Возвращает список игроков с их балансами кошелька и банка, отсортированный по общему богатству."""
    verify_owner(owner_id)
    res = await session.scalars(
        select(Player)
        .order_by((Player.wallet + Player.bank_balance).desc())
        .limit(limit)
    )
    return list(res.all())


async def get_player_wealth_details(
    session: AsyncSession, *, owner_id: int, target_id: int
) -> Player | None:
    """Возвращает данные о балансах конкретного игрока."""
    verify_owner(owner_id)
    return await session.get(Player, target_id)


async def set_chat_bot_state(
    session: AsyncSession,
    *,
    owner_id: int,
    chat_id: int,
    is_active: bool,
) -> Chat:
    """Останавливает или возобновляет работу бота в конкретном чате."""
    verify_owner(owner_id)
    chat = await session.get(Chat, chat_id)
    if not chat:
        raise ValueError(f"Чат с ID {chat_id} не найден")

    chat.is_active = is_active
    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=owner_id,
        target_id=None,
        action="set_chat_active",
        reason=f"Бот {'включен' if is_active else 'остановлен'} владельцем",
        details={"is_active": is_active},
    )
    session.add(log_entry)
    await session.flush()
    return chat


async def set_player_global_ban(
    session: AsyncSession,
    *,
    owner_id: int,
    target_id: int,
    banned: bool,
    reason: str | None = None,
) -> Player:
    """Глобальный бан или разбан игрока владельцем."""
    verify_owner(owner_id)
    player = await session.scalar(
        select(Player).where(Player.telegram_id == target_id).with_for_update()
    )
    if not player:
        raise ValueError(f"Игрок с Telegram ID {target_id} не найден")

    player.is_globally_banned = banned
    player.global_ban_reason = reason if banned else None

    log_entry = AdminActionLog(
        chat_id=None,
        actor_id=owner_id,
        target_id=target_id,
        action="global_ban" if banned else "global_unban",
        reason=reason,
        details={"is_globally_banned": banned},
    )
    session.add(log_entry)
    await session.flush()
    return player


async def reset_global_economy(
    session: AsyncSession,
    *,
    owner_id: int,
) -> int:
    """Глобальный сброс экономики: обнуляет кошельки, банк и акции всех игроков."""
    verify_owner(owner_id)

    # Логируем операцию аудита перед сбросом
    log_entry = AdminActionLog(
        chat_id=None,
        actor_id=owner_id,
        target_id=None,
        action="economy_reset",
        reason="Полный сброс глобальной экономики владельцем",
        details={"executed_at": datetime.now(timezone.utc).isoformat()},
    )
    session.add(log_entry)

    # Атомарно обновляем всех игроков
    res = await session.execute(
        update(Player).values(
            wallet=0,
            bank_balance=0,
            shares=0,
        )
    )
    await session.flush()
    return res.rowcount or 0


async def record_owner_broadcast(
    session: AsyncSession,
    *,
    owner_id: int,
    text: str,
    total_chats: int,
    sent_count: int,
    failed_count: int,
    results: dict,
) -> OwnerBroadcast:
    """Сохраняет отчет о рассылке владельца."""
    verify_owner(owner_id)
    broadcast = OwnerBroadcast(
        owner_id=owner_id,
        message_text=text,
        total_chats=total_chats,
        sent_count=sent_count,
        failed_count=failed_count,
        results=results,
    )
    session.add(broadcast)
    await session.flush()
    return broadcast


async def _get_broadcast_state(session: AsyncSession) -> dict:
    row = await session.get(GlobalSetting, _BROADCAST_STATE_KEY)
    if row is None or not isinstance(row.value, dict):
        return {}
    return dict(row.value)


async def get_broadcast_cooldown(
    session: AsyncSession,
    *,
    owner_id: int,
) -> tuple[bool, int]:
    """Проверяет, можно ли сейчас делать рассылку.

    Возвращает (можно_ли, секунд_до_следующей_рассылки). Рассылка разрешена не
    чаще одного раза в 24 часа.
    """
    verify_owner(owner_id)
    state = await _get_broadcast_state(session)
    last_raw = state.get("last_broadcast_at")
    if not last_raw:
        return True, 0
    try:
        last_dt = datetime.fromisoformat(last_raw)
    except (TypeError, ValueError):
        return True, 0
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)

    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
    remaining = int(BROADCAST_COOLDOWN_SECONDS - elapsed)
    if remaining > 0:
        return False, remaining
    return True, 0


async def mark_broadcast_sent(
    session: AsyncSession,
    *,
    owner_id: int,
    now: datetime | None = None,
) -> None:
    """Отмечает факт рассылки, запуская 24-часовой кулдаун."""
    verify_owner(owner_id)
    moment = (now or datetime.now(timezone.utc)).isoformat()
    state = await _get_broadcast_state(session)
    state["last_broadcast_at"] = moment
    stmt = insert(GlobalSetting).values(key=_BROADCAST_STATE_KEY, value=state)
    stmt = stmt.on_conflict_do_update(
        index_elements=[GlobalSetting.key], set_={"value": state}
    )
    await session.execute(stmt)


async def list_broadcast_chats(
    session: AsyncSession,
    *,
    owner_id: int,
) -> list[Chat]:
    """Возвращает активные групповые чаты для рассылки владельца."""
    verify_owner(owner_id)
    res = await session.scalars(
        select(Chat).where(
            Chat.is_active.is_(True),
            Chat.chat_type.in_(["group", "supergroup"]),
        )
    )
    return list(res.all())


def format_cooldown_remaining(seconds: int) -> str:
    """Форматирует оставшееся время кулдауна в часы и минуты."""
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."
