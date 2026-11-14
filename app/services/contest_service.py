"""Сервис конкурсов Fable Randomizer: создание, управление, участие и подписки.

Правила из ТЗ:
- Категории конкурсов: активные, скоро начнутся, завершённые.
- Участие возможно только при подписке на все обязательные каналы и выполнении
  условий; заблокированные администратором игроки участвовать не могут.
- Повторное участие запрещено; отмена участия возможна до завершения конкурса,
  если администратор это разрешил; история при этом сохраняется.
- Данные о подписках проверяются через Telegram Bot API.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Channel,
    Contest,
    ContestChannel,
    ContestEntry,
    Winner,
)
from app.services.player_service import get_or_create_player

logger = logging.getLogger("fable.contest")


class ContestError(Exception):
    """Ошибка конкурсной логики."""


class ContestNotFoundError(ContestError):
    pass


class NotSubscribedError(ContestError):
    def __init__(self, missing: list[Channel]):
        self.missing = missing
        super().__init__("Для участия необходимо подписаться на все указанные каналы")


class AlreadyParticipatingError(ContestError):
    pass


class ContestClosedError(ContestError):
    pass


class BannedFromContestError(ContestError):
    pass


# Статусы конкурса.
STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_ACTIVE = "active"
STATUS_FINISHED = "finished"
STATUS_CANCELLED = "cancelled"
STATUS_ARCHIVED = "archived"

STATUS_LABELS = {
    STATUS_DRAFT: "📝 Черновик",
    STATUS_SCHEDULED: "⏳ Скоро начнётся",
    STATUS_ACTIVE: "🟢 Активный",
    STATUS_FINISHED: "🏁 Завершён",
    STATUS_CANCELLED: "❌ Отменён",
    STATUS_ARCHIVED: "📦 В архиве",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ===================== CRUD =====================

async def create_contest(
    session: AsyncSession,
    *,
    title: str,
    description: str = "",
    image_url: str | None = None,
    prize: str | None = None,
    winners_count: int = 1,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    extra_conditions: str | None = None,
    post_text: str | None = None,
    allow_leave: bool = True,
    status: str = STATUS_DRAFT,
    created_by: int | None = None,
) -> Contest:
    contest = Contest(
        title=title.strip()[:200],
        description=description or "",
        image_url=image_url,
        prize=prize,
        winners_count=max(1, int(winners_count)),
        starts_at=starts_at,
        ends_at=ends_at,
        extra_conditions=extra_conditions,
        post_text=post_text,
        allow_leave=allow_leave,
        status=status,
        created_by=created_by,
    )
    session.add(contest)
    await session.flush()
    return contest


async def get_contest(session: AsyncSession, contest_id: int) -> Contest | None:
    return await session.get(Contest, contest_id)


async def require_contest(session: AsyncSession, contest_id: int) -> Contest:
    contest = await session.get(Contest, contest_id)
    if contest is None:
        raise ContestNotFoundError(f"Конкурс {contest_id} не найден")
    return contest


async def update_contest(session: AsyncSession, contest_id: int, **fields) -> Contest:
    contest = await require_contest(session, contest_id)
    for key, value in fields.items():
        if hasattr(contest, key):
            setattr(contest, key, value)
    await session.flush()
    return contest


async def delete_contest(session: AsyncSession, contest_id: int) -> None:
    """Полное удаление конкурса (вместе со связями, участниками и победителями)."""
    contest = await require_contest(session, contest_id)
    await session.delete(contest)
    await session.flush()


async def archive_contest(session: AsyncSession, contest_id: int) -> Contest:
    contest = await require_contest(session, contest_id)
    contest.is_archived = True
    contest.status = STATUS_ARCHIVED
    await session.flush()
    return contest


def compute_status(contest: Contest) -> str:
    """Вычисляет актуальный статус конкурса с учётом дат (если он не отменён/архив)."""
    if contest.status in (STATUS_CANCELLED, STATUS_ARCHIVED, STATUS_FINISHED):
        return contest.status
    now = _now()
    starts = _as_aware(contest.starts_at)
    ends = _as_aware(contest.ends_at)
    if ends and now > ends:
        return STATUS_FINISHED
    if starts and now < starts:
        return STATUS_SCHEDULED
    if contest.status == STATUS_DRAFT:
        return STATUS_DRAFT
    return STATUS_ACTIVE


async def sync_statuses(session: AsyncSession) -> int:
    """Синхронизирует статусы всех конкурсов по датам. Возвращает число изменённых."""
    contests = (await session.scalars(select(Contest))).all()
    changed = 0
    for contest in contests:
        new_status = compute_status(contest)
        if new_status != contest.status and contest.status not in (STATUS_ARCHIVED,):
            contest.status = new_status
            changed += 1
    if changed:
        await session.flush()
    return changed


async def list_contests(session: AsyncSession, *, category: str = "all", include_archived: bool = False) -> list[Contest]:
    """Список конкурсов по категории: all | active | upcoming | finished."""
    await sync_statuses(session)
    stmt = select(Contest).order_by(Contest.created_at.desc())
    contests = list((await session.scalars(stmt)).all())
    if not include_archived:
        contests = [c for c in contests if not c.is_archived]
    if category == "active":
        contests = [c for c in contests if c.status == STATUS_ACTIVE]
    elif category == "upcoming":
        contests = [c for c in contests if c.status == STATUS_SCHEDULED]
    elif category == "finished":
        contests = [c for c in contests if c.status in (STATUS_FINISHED, STATUS_CANCELLED)]
    return contests


async def participants_count(session: AsyncSession, contest_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(ContestEntry.id)).where(
                ContestEntry.contest_id == contest_id,
                ContestEntry.status == "active",
            )
        )
        or 0
    )


async def winners_count(session: AsyncSession, contest_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(Winner.id)).where(Winner.contest_id == contest_id)
        )
        or 0
    )


async def list_entries(session: AsyncSession, contest_id: int, *, active_only: bool = True) -> list[ContestEntry]:
    stmt = select(ContestEntry).where(ContestEntry.contest_id == contest_id)
    if active_only:
        stmt = stmt.where(ContestEntry.status == "active")
    stmt = stmt.order_by(ContestEntry.joined_at.asc())
    return list((await session.scalars(stmt)).all())


async def get_entry(session: AsyncSession, contest_id: int, user_id: int) -> ContestEntry | None:
    return await session.scalar(
        select(ContestEntry).where(
            ContestEntry.contest_id == contest_id, ContestEntry.user_id == user_id
        )
    )


# ===================== КАНАЛЫ КОНКУРСА =====================

async def set_contest_channels(session: AsyncSession, contest_id: int, channel_ids: list[int]) -> None:
    """Полностью заменяет список обязательных каналов конкурса."""
    await require_contest(session, contest_id)
    existing = (await session.scalars(
        select(ContestChannel).where(ContestChannel.contest_id == contest_id)
    )).all()
    for link in existing:
        await session.delete(link)
    await session.flush()
    for cid in dict.fromkeys(channel_ids):
        session.add(ContestChannel(contest_id=contest_id, channel_id=cid))
    await session.flush()


async def add_contest_channel(session: AsyncSession, contest_id: int, channel_id: int) -> None:
    await require_contest(session, contest_id)
    existing = await session.scalar(
        select(ContestChannel).where(
            ContestChannel.contest_id == contest_id, ContestChannel.channel_id == channel_id
        )
    )
    if existing is None:
        session.add(ContestChannel(contest_id=contest_id, channel_id=channel_id))
        await session.flush()


async def remove_contest_channel(session: AsyncSession, contest_id: int, channel_id: int) -> bool:
    link = await session.scalar(
        select(ContestChannel).where(
            ContestChannel.contest_id == contest_id, ContestChannel.channel_id == channel_id
        )
    )
    if link is None:
        return False
    await session.delete(link)
    await session.flush()
    return True


async def get_contest_channels(session: AsyncSession, contest_id: int) -> list[Channel]:
    rows = (
        await session.scalars(
            select(Channel)
            .join(ContestChannel, ContestChannel.channel_id == Channel.id)
            .where(ContestChannel.contest_id == contest_id, Channel.is_active.is_(True))
            .order_by(Channel.id.asc())
        )
    ).all()
    return list(rows)


# ===================== ПОДПИСКИ =====================

def _is_member_status(status: str | None) -> bool:
    return status in ("member", "administrator", "creator")


async def check_subscription(bot: Bot, channel: Channel, user_id: int) -> bool:
    """Проверяет подписку пользователя на канал через Telegram Bot API.

    Возвращает True только при уверенной подписке. При ошибках API (бот не админ,
    канал недоступен) — False, что безопаснее для конкурса.
    """
    if channel.chat_id is None:
        return True  # без привязки к chat_id проверить нельзя — не блокируем
    try:
        member = await bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
        status = getattr(member, "status", None)
        if _is_member_status(status):
            return True
        if status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False
    except Exception as e:  # noqa: BLE001 — любые ошибки API считаем "не подписан"
        logger.warning("Не удалось проверить подписку на канал %s: %s", channel.chat_id, e)
        return False


async def check_all_subscriptions(
    session: AsyncSession, bot: Bot, *, contest_id: int, user_id: int
) -> list[Channel]:
    """Возвращает список каналов, на которые пользователь ещё не подписан."""
    channels = await get_contest_channels(session, contest_id)
    missing: list[Channel] = []
    for ch in channels:
        if not await check_subscription(bot, ch, user_id):
            missing.append(ch)
    return missing


# ===================== УЧАСТИЕ =====================

async def join_contest(
    session: AsyncSession,
    bot: Bot,
    *,
    contest_id: int,
    user_id: int,
    first_name: str = "",
    username: str | None = None,
    bonus_units: int = 0,
) -> ContestEntry:
    """Регистрирует игрока в конкурсе после проверки всех условий.

    Вызывает исключения ContestClosedError / AlreadyParticipatingError /
    BannedFromContestError / NotSubscribedError.
    """
    contest = await require_contest(session, contest_id)
    status = compute_status(contest)
    if status not in (STATUS_ACTIVE, STATUS_SCHEDULED):
        raise ContestClosedError("Конкурс недоступен для участия")

    player = (await get_or_create_player(
        session, telegram_id=user_id, first_name=first_name or "Игрок", username=username
    )).player
    if player.is_globally_banned:
        raise BannedFromContestError("Вы заблокированы администратором и не можете участвовать")

    existing = await get_entry(session, contest_id, user_id)
    if existing is not None and existing.status == "active":
        raise AlreadyParticipatingError("Ты уже участвуешь в этом конкурсе!")

    # Проверка подписок (для конкурса без каналов список пуст).
    missing = await check_all_subscriptions(session, bot, contest_id=contest_id, user_id=user_id)
    if missing:
        raise NotSubscribedError(missing)

    if existing is not None:
        existing.status = "active"
        existing.left_at = None
        existing.bonus_weight = bonus_units
        entry = existing
    else:
        entry = ContestEntry(
            contest_id=contest_id,
            user_id=user_id,
            status="active",
            bonus_weight=bonus_units,
        )
        session.add(entry)
    await session.flush()
    return entry


async def leave_contest(session: AsyncSession, *, contest_id: int, user_id: int) -> bool:
    """Отменяет участие игрока до завершения конкурса (если это разрешено)."""
    contest = await require_contest(session, contest_id)
    status = compute_status(contest)
    if status not in (STATUS_ACTIVE, STATUS_SCHEDULED):
        raise ContestClosedError("После завершения конкурса отмена участия невозможна")
    if not contest.allow_leave:
        raise ContestError("В этом конкурсе отмена участия не предусмотрена")

    entry = await get_entry(session, contest_id, user_id)
    if entry is None or entry.status != "active":
        return False
    entry.status = "left"
    entry.left_at = _now()
    await session.flush()
    return True


async def get_user_entries(session: AsyncSession, user_id: int, *, filter_: str = "all") -> list[tuple[ContestEntry, Contest]]:
    """История участия игрока с фильтрами all | active | finished | wins."""
    rows = (
        await session.execute(
            select(ContestEntry, Contest)
            .join(Contest, Contest.id == ContestEntry.contest_id)
            .where(ContestEntry.user_id == user_id)
            .order_by(ContestEntry.joined_at.desc())
        )
    ).all()
    result: list[tuple[ContestEntry, Contest]] = []
    for entry, contest in rows:
        st = compute_status(contest)
        if filter_ == "active" and not (entry.status == "active" and st in (STATUS_ACTIVE, STATUS_SCHEDULED)):
            continue
        if filter_ == "finished" and st not in (STATUS_FINISHED, STATUS_CANCELLED):
            continue
        if filter_ == "wins":
            if not await _is_winner(session, contest.id, user_id):
                continue
        result.append((entry, contest))
    return result


async def _is_winner(session: AsyncSession, contest_id: int, user_id: int) -> bool:
    row = await session.scalar(
        select(Winner.id).where(Winner.contest_id == contest_id, Winner.user_id == user_id).limit(1)
    )
    return row is not None


async def is_winner(session: AsyncSession, contest_id: int, user_id: int) -> bool:
    return await _is_winner(session, contest_id, user_id)


async def get_user_stats(session: AsyncSession, user_id: int) -> dict:
    """Сводная статистика участия игрока."""
    entries = (await session.scalars(
        select(ContestEntry).where(ContestEntry.user_id == user_id)
    )).all()
    active = 0
    finished = 0
    cancelled = 0
    for entry in entries:
        contest = await session.get(Contest, entry.contest_id)
        if contest is None:
            continue
        st = compute_status(contest)
        if entry.status == "left":
            cancelled += 1
        elif st in (STATUS_ACTIVE, STATUS_SCHEDULED):
            active += 1
        elif st in (STATUS_FINISHED, STATUS_CANCELLED):
            finished += 1
    wins = int(
        await session.scalar(select(func.count(Winner.id)).where(Winner.user_id == user_id)) or 0
    )
    return {"active": active, "finished": finished, "cancelled": cancelled, "wins": wins}
