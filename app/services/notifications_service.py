"""Автоматические уведомления Fable Randomizer.

Правила из ТЗ:
- Уведомления о новом конкурсе, старте, скором окончании, завершении, выборе
  победителей, успешной регистрации, необходимости подписаться, изменении условий
  и отмене конкурса.
- Отправляются только тем, кому они действительно нужны.
- Одно событие не отправляется дважды (уникальный ключ в таблице notifications).
- Администратор может отключать отдельные типы уведомлений.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ContestEntry, Notification
from app.services.randomizer_settings import is_notification_enabled

logger = logging.getLogger("fable.notifications")


async def _already_sent(session: AsyncSession, user_id: int, event_key: str) -> bool:
    row = await session.scalar(
        select(Notification.id).where(
            Notification.user_id == user_id, Notification.event_key == event_key
        )
    )
    return row is not None


async def _mark_sent(
    session: AsyncSession, *, user_id: int, event_key: str, event_type: str, contest_id: int | None
) -> None:
    try:
        async with session.begin_nested():
            session.add(
                Notification(
                    user_id=user_id,
                    event_key=event_key,
                    event_type=event_type,
                    contest_id=contest_id,
                )
            )
            await session.flush()
    except Exception:  # noqa: BLE001 — уже помечено (уникальный ключ user_id+event_key)
        pass


async def notify_user(
    session: AsyncSession,
    bot: Bot,
    *,
    user_id: int,
    event_type: str,
    event_key: str,
    text: str,
    contest_id: int | None = None,
    reply_markup=None,
) -> bool:
    """Отправляет уведомление конкретному пользователю с защитой от повторов.

    Возвращает True, если сообщение было отправлено.
    """
    if not await is_notification_enabled(session, event_type):
        return False
    if await _already_sent(session, user_id, event_key):
        return False
    try:
        await bot.send_message(
            chat_id=user_id, text=text, parse_mode="HTML", reply_markup=reply_markup
        )
    except Exception as e:  # пользователь мог заблокировать бота — это не критично
        logger.info("Не удалось отправить уведомление %s игроку %s: %s", event_type, user_id, e)
        return False
    await _mark_sent(
        session, user_id=user_id, event_key=event_key, event_type=event_type, contest_id=contest_id
    )
    return True


async def notify_contest_participants(
    session: AsyncSession,
    bot: Bot,
    *,
    contest_id: int,
    event_type: str,
    event_key_suffix: str,
    text_builder,
    active_only: bool = True,
) -> int:
    """Рассылает уведомление участникам конкурса. Возвращает число отправленных."""
    stmt = select(ContestEntry.user_id).where(ContestEntry.contest_id == contest_id)
    if active_only:
        stmt = stmt.where(ContestEntry.status == "active")
    user_ids = [int(x) for x in (await session.scalars(stmt)).all()]

    sent = 0
    for uid in user_ids:
        text = text_builder(uid) if callable(text_builder) else text_builder
        if await notify_user(
            session,
            bot,
            user_id=uid,
            event_type=event_type,
            event_key=f"{event_type}:{contest_id}:{event_key_suffix}",
            text=text,
            contest_id=contest_id,
        ):
            sent += 1
    return sent


async def notify_new_contest(session: AsyncSession, bot: Bot, *, contest_id: int, title: str, prize: str | None) -> int:
    """Уведомляет всех зарегистрированных игроков о новом конкурсе."""
    from app.db.models import Player

    prize_line = f"\n🎁 Приз: <b>{prize}</b>" if prize else ""
    text = (
        f"🎁 <b>Новый конкурс Fable Randomizer!</b>\n\n"
        f"«{title}»{prize_line}\n\n"
        f"Загляни в раздел «🎁 Конкурсы», чтобы участвовать!"
    )
    user_ids = [int(x) for x in (await session.scalars(select(Player.telegram_id))).all()]
    sent = 0
    for uid in user_ids:
        if await notify_user(
            session,
            bot,
            user_id=uid,
            event_type="new_contest",
            event_key=f"new_contest:{contest_id}",
            text=text,
            contest_id=contest_id,
        ):
            sent += 1
    return sent


async def notify_winners_selected(
    session: AsyncSession, bot: Bot, *, contest_id: int, title: str, winner_ids: list[int], prize: str | None
) -> int:
    """Уведомляет победителей о выигрыше."""
    sent = 0
    prize_line = f"\n🎁 Твой приз: <b>{prize}</b>" if prize else ""
    for uid in winner_ids:
        text = (
            f"🏆 <b>Поздравляем!</b>\n\n"
            f"Ты выиграл в конкурсе «{title}»!{prize_line}\n\n"
            f"Следи за новостями Fable Randomizer!"
        )
        if await notify_user(
            session,
            bot,
            user_id=uid,
            event_type="winners_selected",
            event_key=f"winners_selected:{contest_id}",
            text=text,
            contest_id=contest_id,
        ):
            sent += 1
    return sent
