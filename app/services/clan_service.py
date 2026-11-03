"""Сервис кланов Fable.

Правила из ТЗ:
- Кланы глобальные и общие для всех групп.
- Один клан на игрока во всей системе.
- Команды:
  * "клан создать Название" — проверка уникальности, создатель становится главой.
  * "клан" — просмотр клана, главы, списка участников, рейтинга.
  * "клан вступить Название" — вступление в открытый клан.
  * "клан чат текст" — отправка сообщения участникам клана, доступно независимо от чата.
  * "клан удалить" — удаление клана ТОЛЬКО его создателем. Ни админы чатов, ни владелец бота не могут удалить чужой клан вместо создателя. Требует подтверждения.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Clan, ClanAudit, ClanMember, ClanMessage, Player


class ClanError(Exception):
    """Базовая ошибка кланов."""


class ClanAlreadyExistsError(ClanError):
    """Клан с таким именем уже существует."""


class AlreadyInClanError(ClanError):
    """Игрок уже состоит в клане."""


class ClanNotFoundError(ClanError):
    """Клан не найден."""


class ClanAccessDeniedError(ClanError):
    """Недостаточно прав для управления кланом."""


@dataclass(frozen=True, slots=True)
class ClanInfo:
    clan: Clan
    creator_name: str
    member_count: int
    members: list[tuple[Player, str]]  # (player, role)


def _normalize_clan_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip()
    if not (2 <= len(cleaned) <= 40):
        raise ClanError("Название клана должно содержать от 2 до 40 символов")
    return cleaned


async def create_clan(
    session: AsyncSession,
    *,
    creator_id: int,
    name: str,
) -> Clan:
    """Создает новый клан. Создатель становится главой (role='leader')."""
    norm_name = _normalize_clan_name(name)
    key_name = norm_name.casefold()

    # Проверяем, состоит ли игрок уже в каком-либо клане
    existing_membership = await session.scalar(
        select(ClanMember).where(ClanMember.user_id == creator_id)
    )
    if existing_membership:
        raise AlreadyInClanError("Вы уже состоите в клане. Сначала покиньте или удалите текущий клан.")

    # Проверяем уникальность названия
    existing_clan = await session.scalar(
        select(Clan).where(Clan.normalized_name == key_name, Clan.deleted_at.is_(None))
    )
    if existing_clan:
        raise ClanAlreadyExistsError(f"Клан с названием «{norm_name}» уже существует")

    clan = Clan(
        name=norm_name,
        normalized_name=key_name,
        creator_id=creator_id,
        level=1,
        rating=0,
        is_open=True,
    )
    session.add(clan)
    await session.flush()

    member = ClanMember(
        clan_id=clan.id,
        user_id=creator_id,
        role="leader",
    )
    session.add(member)

    audit = ClanAudit(
        clan_id=clan.id,
        clan_name=clan.name,
        actor_id=creator_id,
        action="create",
        details={"name": norm_name},
    )
    session.add(audit)
    await session.flush()
    return clan


async def get_player_clan(
    session: AsyncSession, *, user_id: int
) -> ClanInfo | None:
    """Возвращает информацию о клане, в котором состоит игрок."""
    member_rec = await session.scalar(
        select(ClanMember).where(ClanMember.user_id == user_id)
    )
    if not member_rec:
        return None

    clan = await session.scalar(
        select(Clan).where(Clan.id == member_rec.clan_id, Clan.deleted_at.is_(None))
    )
    if not clan:
        return None

    creator = await session.scalar(
        select(Player).where(Player.telegram_id == clan.creator_id)
    )
    creator_name = creator.first_name if creator else "Неизвестно"

    members_res = (
        await session.execute(
            select(Player, ClanMember.role)
            .join(ClanMember, ClanMember.user_id == Player.telegram_id)
            .where(ClanMember.clan_id == clan.id)
            .order_by(desc(ClanMember.role == "leader"), Player.telegram_id)
        )
    ).all()

    members = [(row[0], row[1]) for row in members_res]

    return ClanInfo(
        clan=clan,
        creator_name=creator_name,
        member_count=len(members),
        members=members,
    )


async def join_clan(
    session: AsyncSession,
    *,
    user_id: int,
    clan_name: str,
) -> Clan:
    """Вступление игрока в открытый клан."""
    norm_name = _normalize_clan_name(clan_name).casefold()

    existing_membership = await session.scalar(
        select(ClanMember).where(ClanMember.user_id == user_id)
    )
    if existing_membership:
        raise AlreadyInClanError("Вы уже состоите в клане. Сначала покиньте текущий клан.")

    clan = await session.scalar(
        select(Clan).where(Clan.normalized_name == norm_name, Clan.deleted_at.is_(None))
    )
    if not clan:
        raise ClanNotFoundError(f"Клан «{clan_name}» не найден")

    if not clan.is_open:
        raise ClanError("Вступление в этот клан закрыто")

    member = ClanMember(clan_id=clan.id, user_id=user_id, role="member")
    session.add(member)

    audit = ClanAudit(
        clan_id=clan.id,
        clan_name=clan.name,
        actor_id=user_id,
        action="join",
        details={},
    )
    session.add(audit)
    await session.flush()
    return clan


async def send_clan_message(
    session: AsyncSession,
    *,
    user_id: int,
    text: str,
) -> ClanMessage:
    """Отправка сообщения в клановый чат."""
    cleaned = text.strip()
    if not cleaned:
        raise ClanError("Сообщение не может быть пустым")

    member = await session.scalar(
        select(ClanMember).where(ClanMember.user_id == user_id)
    )
    if not member:
        raise ClanError("Вы не состоите в клане")

    player = await session.scalar(
        select(Player).where(Player.telegram_id == user_id)
    )
    sender_name = player.first_name if player else f"ID {user_id}"

    msg = ClanMessage(
        clan_id=member.clan_id,
        sender_id=user_id,
        sender_name=sender_name,
        text=cleaned,
    )
    session.add(msg)
    await session.flush()
    return msg


async def get_clan_messages(
    session: AsyncSession, *, user_id: int, limit: int = 10
) -> list[ClanMessage]:
    """Возвращает историю сообщений кланового чата."""
    member = await session.scalar(
        select(ClanMember).where(ClanMember.user_id == user_id)
    )
    if not member:
        raise ClanError("Вы не состоите в клане")

    res = await session.scalars(
        select(ClanMessage)
        .where(ClanMessage.clan_id == member.clan_id)
        .order_by(desc(ClanMessage.created_at))
        .limit(limit)
    )
    return list(reversed(res.all()))


async def delete_clan(
    session: AsyncSession,
    *,
    user_id: int,
) -> str:
    """Удаление клана ТОЛЬКО его создателем."""
    member = await session.scalar(
        select(ClanMember).where(ClanMember.user_id == user_id)
    )
    if not member:
        raise ClanError("Вы не состоите в клане")

    clan = await session.scalar(
        select(Clan).where(Clan.id == member.clan_id, Clan.deleted_at.is_(None)).with_for_update()
    )
    if not clan:
        raise ClanNotFoundError("Клан не найден")

    # Строгое правило ТЗ 5.5: Удалить клан может только его создатель.
    if clan.creator_id != user_id:
        raise ClanAccessDeniedError("Только создатель клана может удалить его!")

    clan_name = clan.name
    now = datetime.now(timezone.utc)
    clan.deleted_at = now
    clan.normalized_name = f"{clan.normalized_name}_deleted_{int(now.timestamp())}"

    # Удаляем всех участников из клана
    await session.execute(
        delete(ClanMember).where(ClanMember.clan_id == clan.id)
    )

    audit = ClanAudit(
        clan_id=clan.id,
        clan_name=clan_name,
        actor_id=user_id,
        action="delete",
        details={},
    )
    session.add(audit)
    await session.flush()
    return clan_name
