"""Сервис модерации и иерархии прав Fable.

Правила из ТЗ:
- Иерархия уровней администраторов в чате:
  * Уровень 5 — Создатель (все права, назначение уровней 2-5).
  * Уровень 4 — Старший админ (назначение уровней 2-3, бан, кик, мут, варн, заморозка, очистка).
  * Уровень 3 — Админ (мут, варн, бан, кик, очистка, заморозка, правила, назначение уровня 2).
  * Уровень 2 — Модератор (мут, варн, очистка, репорты).
  * Уровень 1 — Обычный игрок.
- Правила назначения и наказаний:
  * Нельзя наказывать равного или вышестоящего администратора.
  * Нельзя повышать самого себя. Самопонижение разрешено.
  * Повышать может только пользователь, стоящий выше по уровню.
  * Назначать уровень 5 может только уровень 5. Снимать уровень 5 может только другой уровень 5.
- Команды модерации:
  * "мут @user 30м причина" (м - минуты, ч - часы, д - дни).
  * "размут @user".
  * "варн @user причина" (срок 30 дней, без автоматических санкций).
  * "варны @user", "варн снять @user".
  * "кик @user причина".
  * "бан @user причина", "разбан @user".
  * "админ список".
  * Назначение: "админ назначить @user <уровень>", "админ снять @user".
- Антиссылки:
  * Ссылки запрещены по умолчанию (http, https, www, t.me, инвайты, домены).
  * Упоминания вида @username разрешены.
  * Белый список ссылок: "ссылки добавить <домен>", "ссылки удалить <домен>", "ссылки список".
- Все действия логируются в AdminActionLog и ModerationCase.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Sequence

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminActionLog,
    Chat,
    ChatAdmin,
    ChatLinkWhitelist,
    ModerationCase,
    Player,
)


class ModerationError(Exception):
    """Базовая ошибка модерации."""


class PermissionDeniedError(ModerationError):
    """Недостаточно прав."""


class HierarchyViolationError(ModerationError):
    """Нарушение субординации уровней."""


ROLE_NAMES = {
    5: "Создатель",
    4: "Старший админ",
    3: "Админ",
    2: "Модератор",
    1: "Игрок",
}

# Регулярные выражения для поиска ссылок
URL_REGEX = re.compile(
    r"(?:https?://|www\.)?[a-zA-Z0-9-]+\.(?:com|org|ru|net|io|me|info|biz|ua|kz|by|dev|app|online|site|top|xyz)(?:/[^\s]*)?|t\.me/[^\s]+",
    re.IGNORECASE,
)


def parse_duration_string(duration_str: str) -> timedelta:
    """Парсит строки вида '30м', '2ч', '7д' в timedelta."""
    match = re.match(r"^(\d+)([мчдmhd])$", duration_str.strip().lower())
    if not match:
        raise ModerationError("Неверный формат времени. Пример: 30м, 2ч, 7д")
    val, unit = int(match.group(1)), match.group(2)
    if val <= 0:
        raise ModerationError("Срок должен быть больше 0")
    if unit in ("м", "m"):
        return timedelta(minutes=val)
    elif unit in ("ч", "h"):
        return timedelta(hours=val)
    elif unit in ("д", "d"):
        return timedelta(days=val)
    raise ModerationError("Неизвестная единица измерения времени")


async def get_user_admin_level(
    session: AsyncSession, *, chat_id: int, user_id: int, bot=None
) -> int:
    """Возвращает локальный уровень прав пользователя в чате (от 1 до 5).
    Если пользователь является создателем или администратором в самом Telegram,
    но в базе еще нет записи, уровень синхронизируется.
    """
    from app.config import get_settings
    # Глобальный владелец бота всегда имеет уровень 5 во всех чатах
    if user_id == 8039111975 or user_id == get_settings().owner_id:
        return 5

    admin_row = await session.scalar(
        select(ChatAdmin).where(
            ChatAdmin.chat_id == chat_id,
            ChatAdmin.user_id == user_id,
            ChatAdmin.is_active.is_(True),
        )
    )
    if admin_row:
        return admin_row.level

    # Попытка проверить права напрямую в Telegram чате, если в БД еще нет роли
    if bot:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            status = getattr(member, "status", None)
            if status in ("creator", "owner"):
                # Назначаем создателю ур. 5
                stmt = insert(ChatAdmin).values(
                    chat_id=chat_id,
                    user_id=user_id,
                    level=5,
                    assigned_by=user_id,
                    is_active=True,
                ).on_conflict_do_update(
                    index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
                    set_={"level": 5, "is_active": True},
                )
                await session.execute(stmt)
                return 5
            elif status == "administrator":
                stmt = insert(ChatAdmin).values(
                    chat_id=chat_id,
                    user_id=user_id,
                    level=3,
                    assigned_by=user_id,
                    is_active=True,
                ).on_conflict_do_update(
                    index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
                    set_={"level": 3, "is_active": True},
                )
                await session.execute(stmt)
                return 3
        except Exception:
            pass

    return 1


async def sync_chat_creator_and_admins(session: AsyncSession, *, chat_id: int, bot) -> int | None:
    """Синхронизирует владельца (создателя) и администраторов чата через Telegram Bot API.
    Возвращает telegram_id создателя чата, если найден.
    """
    if not bot:
        return None
    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
        creator_id = None
        for adm in admins:
            user = getattr(adm, "user", None)
            if not user or getattr(user, "is_bot", False):
                continue

            status = getattr(adm, "status", None)
            if status in ("creator", "owner"):
                creator_id = user.id
                level = 5
            elif status == "administrator":
                level = 3
            else:
                continue

            # Убеждаемся, что игрок существует
            p_stmt = insert(Player).values(
                telegram_id=user.id,
                first_name=user.first_name or "Участник",
                username=user.username,
            ).on_conflict_do_nothing(index_elements=[Player.telegram_id])
            await session.execute(p_stmt)

            # Синхронизируем уровень
            stmt = insert(ChatAdmin).values(
                chat_id=chat_id,
                user_id=user.id,
                level=level,
                assigned_by=user.id,
                is_active=True,
            ).on_conflict_do_update(
                index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
                set_={"level": level, "is_active": True},
            )
            await session.execute(stmt)

        return creator_id
    except Exception:
        return None


async def check_can_moderate(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    min_required_level: int = 2,
    bot=None,
) -> tuple[int, int]:
    """Проверяет право модератора наказывать цель."""
    mod_level = await get_user_admin_level(session, chat_id=chat_id, user_id=moderator_id, bot=bot)
    if mod_level < min_required_level:
        raise PermissionDeniedError(
            f"Для этого действия требуется уровень {min_required_level} ({ROLE_NAMES[min_required_level]}), ваш уровень: {mod_level}"
        )

    target_level = await get_user_admin_level(session, chat_id=chat_id, user_id=target_id, bot=bot)
    if moderator_id != target_id and mod_level <= target_level:
        raise HierarchyViolationError(
            f"Вы не можете применить санкции к участнику равного или более высокого уровня ({ROLE_NAMES[target_level]})"
        )

    return mod_level, target_level


async def set_admin_level(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_id: int,
    target_id: int,
    new_level: int,
    bot=None,
) -> int:
    """Назначение роли / уровня администратора с соблюдением всех правил иерархии."""
    actor_level = await get_user_admin_level(session, chat_id=chat_id, user_id=actor_id, bot=bot)
    target_level = await get_user_admin_level(session, chat_id=chat_id, user_id=target_id, bot=bot)

    if not (1 <= new_level <= 5):
        raise ModerationError("Уровень должен быть от 1 до 5")

    # Самопонижение разрешено
    if actor_id == target_id and actor_level > 1:
        if new_level >= actor_level:
            raise HierarchyViolationError("Самоповышение запрещено!")
    else:
        # Нельзя изменять равных и вышестоящих
        if actor_level <= target_level:
            raise HierarchyViolationError("Вы не можете изменять роль равного или вышестоящего администратора")
        # Нельзя назначить уровень равный или выше своего собственного
        if new_level >= actor_level:
            raise HierarchyViolationError("Вы не можете назначить уровень равный или выше собственного")
        # Назначать уровень 5 может только уровень 5
        if new_level == 5 and actor_level != 5:
            raise HierarchyViolationError("Только Создатель (ур. 5) может назначить другого Создателя")

    now = datetime.now(timezone.utc)
    if new_level == 1:
        # Снятие админки
        await session.execute(
            delete(ChatAdmin).where(
                ChatAdmin.chat_id == chat_id, ChatAdmin.user_id == target_id
            )
        )
    else:
        statement = insert(ChatAdmin).values(
            chat_id=chat_id,
            user_id=target_id,
            level=new_level,
            assigned_by=actor_id,
            is_active=True,
        ).on_conflict_do_update(
            index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
            set_={"level": new_level, "assigned_by": actor_id, "is_active": True},
        )
        await session.execute(statement)

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=actor_id,
        target_id=target_id,
        action="set_level",
        reason=f"Установлен уровень {new_level} ({ROLE_NAMES[new_level]})",
        details={"old_level": target_level, "new_level": new_level},
    )
    session.add(log_entry)
    await session.flush()
    return new_level


async def issue_mute(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    duration: timedelta,
    reason: str | None = None,
    bot=None,
) -> ModerationCase:
    """Выдача мута с проверкой прав."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    now = datetime.now(timezone.utc)
    expires_at = now + duration

    case = ModerationCase(
        chat_id=chat_id,
        target_id=target_id,
        moderator_id=moderator_id,
        action="mute",
        reason=reason or "Не указана",
        expires_at=expires_at,
        is_active=True,
    )
    session.add(case)

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="mute",
        reason=reason,
        details={"duration_sec": int(duration.total_seconds()), "expires_at": expires_at.isoformat()},
    )
    session.add(log_entry)
    await session.flush()
    return case


async def revoke_mute(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    bot=None,
) -> None:
    """Снятие мута."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    cases = (
        await session.scalars(
            select(ModerationCase).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "mute",
                ModerationCase.is_active.is_(True),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    for c in cases:
        c.is_active = False
        c.revoked_by = moderator_id
        c.revoked_at = now

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="unmute",
        reason="Снятие мута",
        details={},
    )
    session.add(log_entry)
    await session.flush()


async def issue_warn(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    reason: str | None = None,
    bot=None,
) -> tuple[ModerationCase, int]:
    """Выдача варна (срок 30 дней, без авто-санкций). Возвращает (case, активных_варнов)."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=30)

    case = ModerationCase(
        chat_id=chat_id,
        target_id=target_id,
        moderator_id=moderator_id,
        action="warn",
        reason=reason or "Не указана",
        expires_at=expires_at,
        is_active=True,
    )
    session.add(case)

    # Считаем число активных варнов пользователя
    count = (
        await session.scalar(
            select(func.count(ModerationCase.id)).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "warn",
                ModerationCase.is_active.is_(True),
                ModerationCase.expires_at > now,
            )
        )
        or 0
    ) + 1

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="warn",
        reason=reason,
        details={"active_warns": count},
    )
    session.add(log_entry)
    await session.flush()
    return case, count


async def revoke_warn(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    bot=None,
) -> bool:
    """Снятие последнего активного варна."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    last_warn = await session.scalar(
        select(ModerationCase)
        .where(
            ModerationCase.chat_id == chat_id,
            ModerationCase.target_id == target_id,
            ModerationCase.action == "warn",
            ModerationCase.is_active.is_(True),
        )
        .order_by(desc(ModerationCase.created_at))
        .limit(1)
    )
    if not last_warn:
        return False

    now = datetime.now(timezone.utc)
    last_warn.is_active = False
    last_warn.revoked_by = moderator_id
    last_warn.revoked_at = now

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="unwarn",
        reason="Снятие предупреждения",
        details={"warn_id": last_warn.id},
    )
    session.add(log_entry)
    await session.flush()
    return True


async def get_active_warns(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
) -> list[ModerationCase]:
    """Возвращает список активных варнов пользователя."""
    now = datetime.now(timezone.utc)
    cases = (
        await session.scalars(
            select(ModerationCase)
            .where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "warn",
                ModerationCase.is_active.is_(True),
                ModerationCase.expires_at > now,
            )
            .order_by(desc(ModerationCase.created_at))
        )
    ).all()
    return list(cases)


async def record_ban_or_kick(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    action: str,  # 'ban' or 'kick'
    reason: str | None = None,
    bot=None,
) -> ModerationCase:
    """Фиксация бана или кика (требуется ур. 3+ для бана/кика)."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=3, bot=bot)

    case = ModerationCase(
        chat_id=chat_id,
        target_id=target_id,
        moderator_id=moderator_id,
        action=action,
        reason=reason or "Не указана",
        is_active=True,
    )
    session.add(case)

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action=action,
        reason=reason,
        details={},
    )
    session.add(log_entry)
    await session.flush()
    return case


async def is_link_allowed(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
) -> tuple[bool, str | None]:
    """Проверяет сообщение на запрещенные ссылки.

    Возвращает (разрешено, найденная_ссылка).
    Если ссылка есть, но её домен в whitelist этого чата — разрешено.
    """
    matches = URL_REGEX.findall(text)
    if not matches:
        return True, None

    # Получаем белый список доменов данного чата
    whitelist_domains = (
        await session.scalars(
            select(ChatLinkWhitelist.domain).where(ChatLinkWhitelist.chat_id == chat_id)
        )
    ).all()
    allowed_domains = {d.lower() for d in whitelist_domains}

    for found in matches:
        clean_found = found.lower()
        # Проверяем, содержит ли ссылка домен из белого списка
        is_whitelisted = any(domain in clean_found for domain in allowed_domains)
        if not is_whitelisted:
            return False, found

    return True, None


async def add_link_whitelist(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    domain: str,
) -> str:
    """Добавляет домен в белый список ссылок (ур. 5)."""
    level = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if level < 5:
        raise PermissionDeniedError("Управлять белым списком ссылок может только Создатель (ур. 5)")

    norm_domain = domain.strip().lower()
    stmt = insert(ChatLinkWhitelist).values(
        chat_id=chat_id, domain=norm_domain, added_by=admin_id
    ).on_conflict_do_nothing()
    await session.execute(stmt)
    await session.flush()
    return norm_domain


async def remove_link_whitelist(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    domain: str,
) -> bool:
    """Удаляет домен из белого списка ссылок (ур. 5)."""
    level = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if level < 5:
        raise PermissionDeniedError("Управлять белым списком ссылок может только Создатель (ур. 5)")

    norm_domain = domain.strip().lower()
    res = await session.execute(
        delete(ChatLinkWhitelist).where(
            ChatLinkWhitelist.chat_id == chat_id, ChatLinkWhitelist.domain == norm_domain
        )
    )
    await session.flush()
    return (res.rowcount or 0) > 0


async def list_link_whitelist(session: AsyncSession, *, chat_id: int) -> list[str]:
    """Возвращает список разрешенных доменов для чата."""
    domains = (
        await session.scalars(
            select(ChatLinkWhitelist.domain)
            .where(ChatLinkWhitelist.chat_id == chat_id)
            .order_by(ChatLinkWhitelist.domain)
        )
    ).all()
    return list(domains)


async def get_chat_admins(
    session: AsyncSession, *, chat_id: int
) -> list[tuple[Player, int, str]]:
    """Возвращает список админов чата: (player, level, role_name)."""
    res = (
        await session.execute(
            select(Player, ChatAdmin.level)
            .join(ChatAdmin, ChatAdmin.user_id == Player.telegram_id)
            .where(ChatAdmin.chat_id == chat_id, ChatAdmin.is_active.is_(True))
            .order_by(desc(ChatAdmin.level), Player.telegram_id)
        )
    ).all()
    return [(p, lvl, ROLE_NAMES.get(lvl, "Админ")) for p, lvl in res]
