"""Фоновые задачи бота Fable:
1. Суточный расчет процентов банка.
2. Цикл маркетмейкера биржи.
3. Фоновый воркер вовлечения и байтов (в ЛС и в чаты).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import random
from aiogram import Bot

from app.db.session import session_scope
from app.services.bank_service import accrue_daily_interest
from app.services.market_maker import run_market_maker_cycle
from app.services.engagement_service import process_dm_baits, process_chat_baits
from app.services.ai_moderation_service import cleanup_expired_captchas
from app.services.moderation_service import expire_moderation_cases

logger = logging.getLogger(__name__)


async def background_moderation_expiry_worker(bot: Bot, interval_seconds: int = 60) -> None:
    """Автоматически снимает истёкшие муты/баны и закрывает просроченные капчи."""
    while True:
        try:
            async with session_scope() as session:
                expired = await expire_moderation_cases(session, bot=bot)
                if expired:
                    logger.info(f"Автоматически снято истёкших наказаний: {expired}")
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче авто-снятия наказаний: {e}")
        try:
            async with session_scope() as session:
                stale = await cleanup_expired_captchas(session)
                if stale:
                    logger.info(f"Закрыто просроченных капч: {stale}")
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче очистки капч: {e}")
        await asyncio.sleep(interval_seconds)


async def background_bank_interest_worker(interval_seconds: int = 3600) -> None:
    """Периодически проверяет и начисляет суточные проценты банка."""
    while True:
        try:
            async with session_scope() as session:
                results = await accrue_daily_interest(session)
                applied_count = sum(1 for r in results if r.applied)
                total_coins = sum(r.interest for r in results if r.applied)
                if applied_count > 0:
                    logger.info(
                        f"Банковские проценты начислены: "
                        f"обработано {len(results)} счетов, начислено {total_coins} монет {applied_count} игрокам"
                    )
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче банка: {e}")
        await asyncio.sleep(interval_seconds)


async def background_market_maker_worker(interval_seconds: int = 30) -> None:
    """Периодически обновляет котировки и поддерживает ликвидность маркетмейкера."""
    while True:
        try:
            async with session_scope() as session:
                await run_market_maker_cycle(session)
        except Exception as e:
            logger.error(f"Ошибка в фоновом цикле маркетмейкера: {e}")
        await asyncio.sleep(interval_seconds)


async def background_engagement_worker(
    bot: Bot,
    interval_seconds: int | None = None,
    min_interval_seconds: int = 10800,
    max_interval_seconds: int = 21600,
) -> None:
    """Периодически (раз в 3-6 часов) отправляет интерактивные байты в случайные чаты и напоминания в ЛС."""
    while True:
        sleep_duration = (
            interval_seconds
            if interval_seconds is not None
            else random.randint(min_interval_seconds, max_interval_seconds)
        )
        try:
            async with session_scope() as session:
                dm_count = await process_dm_baits(session, bot)
                chat_count = await process_chat_baits(session, bot)
                if dm_count > 0 or chat_count > 0:
                    logger.info(
                        f"Engagement worker: отправлено {dm_count} ЛС-напоминаний и {chat_count} байтов в чаты. "
                        f"Следующий запуск через {sleep_duration // 3600} ч {(sleep_duration % 3600) // 60} мин."
                    )
        except Exception as e:
            logger.error(f"Ошибка в воркере вовлечения: {e}")
        await asyncio.sleep(sleep_duration)


async def background_randomizer_worker(bot: Bot, interval_seconds: int = 300) -> None:
    """Обслуживает конкурсы Randomizer: обновляет статусы, зачитывает рефералы,
    передаёт призы запасным при невыходе на связь и рассылает уведомления
    о старте, скором окончании и завершении конкурсов (без повторных отправок).
    """
    from app.services import contest_service as cs
    from app.services import contest_referral_service as crefs
    from app.services import draw_service as ds
    from app.services import notifications_service as notify
    from app.db.models import Contest

    while True:
        try:
            async with session_scope() as session:
                await cs.sync_statuses(session)
                contests = (await session.scalars(select_all_contests())).all()
                now = datetime.now(timezone.utc)
                # Зачитываем рефералы активных конкурсов (реферал = действие друга).
                for contest in contests:
                    if contest.status == cs.STATUS_ACTIVE:
                        try:
                            await crefs.process_contest_referrals(session, contest.id)
                        except Exception:
                            pass
                # Передаём призы запасным, если основной победитель не вышел на связь.
                for contest in contests:
                    if contest.status != cs.STATUS_FINISHED:
                        continue
                    try:
                        await _process_reserve_timeouts(session, bot, contest)
                    except Exception as e:
                        logger.error(f"Ошибка обработки запасных конкурса {contest.id}: {e}")
                for contest in contests:
                    if contest.status not in (cs.STATUS_ACTIVE, cs.STATUS_SCHEDULED):
                        continue
                    # Уведомление о старте, если конкурс стал активным.
                    if contest.status == cs.STATUS_ACTIVE and contest.starts_at:
                        starts = contest.starts_at
                        if starts.tzinfo is None:
                            starts = starts.replace(tzinfo=timezone.utc)
                        if starts <= now:
                            try:
                                await notify.notify_contest_participants(
                                    session, bot, contest_id=contest.id,
                                    event_type="contest_start", event_key_suffix="auto_start",
                                    text_builder=f"🚀 Конкурс «{contest.title}» начался! Успей принять участие.",
                                )
                            except Exception:
                                pass
                    # Уведомление о скором окончании (менее 24 часов).
                    if contest.ends_at:
                        ends = contest.ends_at
                        if ends.tzinfo is None:
                            ends = ends.replace(tzinfo=timezone.utc)
                        remaining = (ends - now).total_seconds()
                        if 0 < remaining <= 86400:
                            try:
                                await notify.notify_contest_participants(
                                    session, bot, contest_id=contest.id,
                                    event_type="contest_ending", event_key_suffix="auto_ending",
                                    text_builder=f"⏰ Конкурс «{contest.title}» скоро завершится! Успей принять участие.",
                                )
                            except Exception:
                                pass
                    # Уведомление о завершении.
                    if contest.ends_at:
                        ends = contest.ends_at
                        if ends.tzinfo is None:
                            ends = ends.replace(tzinfo=timezone.utc)
                        if ends <= now:
                            try:
                                await notify.notify_contest_participants(
                                    session, bot, contest_id=contest.id,
                                    event_type="contest_finished", event_key_suffix="auto_finished",
                                    text_builder=f"🏁 Конкурс «{contest.title}» завершён. Скоро объявим победителей!",
                                )
                            except Exception:
                                pass
        except Exception as e:
            logger.error(f"Ошибка в воркере Randomizer: {e}")
        await asyncio.sleep(interval_seconds)


async def _process_reserve_timeouts(session, bot: Bot, contest) -> int:
    """Передаёт приз запасному, если основной победитель не подтвердил связь.

    Возвращает число переданных призов. Таймаут (часы) берётся из настроек
    конкурса (`reserve_timeout_hours`, по умолчанию 24).
    """
    from datetime import datetime, timezone

    from app.services import contest_settings as cset
    from app.services import draw_service as ds
    from app.services import notifications_service as notify

    settings = cset.read_settings(contest)
    timeout_hours = int(settings.get("reserve_timeout_hours", 24) or 24)
    pending = await ds.get_pending_main_winners(session, contest.id)
    now = datetime.now(timezone.utc)
    transferred = 0
    for winner in pending:
        notified = winner.notified_at
        if notified is not None and notified.tzinfo is None:
            notified = notified.replace(tzinfo=timezone.utc)
        if notified is not None and (now - notified).total_seconds() < timeout_hours * 3600:
            continue
        reserve = await ds.transfer_to_reserve(session, contest_id=contest.id, main_winner_id=winner.user_id)
        if reserve is None:
            continue
        try:
            await notify.notify_prize_transferred(
                session, bot, contest_id=contest.id, title=contest.title,
                winner_id=reserve.user_id, prize=contest.prize,
            )
        except Exception:
            pass
        transferred += 1
    return transferred


def select_all_contests():
    from sqlalchemy import select

    from app.db.models import Contest

    return select(Contest)
