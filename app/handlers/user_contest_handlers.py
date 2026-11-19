"""Пользовательские конкурсы Fable Randomizer.

Создавать конкурсы может любой пользователь. Если конкурс создаётся для
Telegram-канала, права владельца/администратора проверяются через Telegram Bot
API (``getChatMember``), поэтому подделать владение каналом нельзя. Канал
необязателен: без него конкурс открыт для всех.

Правила:
- владелец конкурса (владелец канала) **не может выиграть** в своём конкурсе;
- обязательная подписка на канал владельца проверяется автоматически;
- владелец видит полную статистику: участники, отмены, конверсия, розыгрыши и
  распределение регистраций по часам/дням (пиковые периоды);
- владелец может завершить конкурс, провести розыгрыш и отменить его.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from aiogram import Router, types, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Player
from app.db.session import session_scope
from app.services import channel_service as chs
from app.services import contest_service as cs
from app.services import contest_settings as cset
from app.services import draw_service as ds
from app.services import notifications_service as notify
from app.services import randomizer_settings as rset
from app.services.audit_service import log_action

logger = logging.getLogger("fable.user_contest")
router = Router(name="user_contest_router")

TRIGGERS_MY = {
    "мои конкурсы", "!мои конкурсы", "mycontests",
    "📣 мои конкурсы",
}
TRIGGERS_CREATE = {
    "создать конкурс", "!создать конкурс", "новый конкурс", "createcontest",
    "➕ создать конкурс",
}


# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

def _skip_kb(extra_buttons: list[tuple[str, str]] | None = None):
    kb = InlineKeyboardBuilder()
    for text, data in (extra_buttons or []):
        kb.button(text=text, callback_data=data)
    kb.button(text="⏭ Пропустить", callback_data="uc_wiz_skip")
    kb.button(text="❌ Отменить", callback_data="uc_wiz_cancel")
    kb.adjust(1)
    return kb.as_markup()


def _parse_dt(text: str) -> datetime | None:
    text = (text or "").strip()
    if text in ("-", "нет", "пропустить", "skip", "—"):
        return None
    fmts = (
        "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d/%m/%Y %H:%M", "%d/%m/%Y",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _status_label(contest) -> str:
    return cs.STATUS_LABELS.get(cs.compute_status(contest), contest.status)


def _hour_bar(value: int, max_value: int, width: int = 10) -> str:
    if max_value <= 0:
        return "░" * width
    filled = max(1, round(value / max_value * width)) if value > 0 else 0
    return "█" * filled + "░" * (width - filled)


def _time_left(contest) -> str:
    """Человекочитаемый остаток времени до окончания конкурса."""
    ends = getattr(contest, "ends_at", None)
    if not ends:
        return "без ограничения"
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=timezone.utc)
    delta = ends - datetime.now(timezone.utc)
    total = int(delta.total_seconds())
    if total <= 0:
        return "время вышло"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} дн. {hours} ч."
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    total = max(1, total)
    filled = min(width, max(0, round(done / total * width)))
    return "█" * filled + "░" * (width - filled)


# ===================== МОИ КОНКУРСЫ =====================

@router.message(Command("mycontests"))
@router.message(F.text.casefold().in_(TRIGGERS_MY))
async def cmd_my_contests(message: types.Message):
    if not message.from_user:
        return
    await _send_my_contests(message, message.from_user.id)


@router.callback_query(F.data == "uc_my_contests")
async def on_my_contests_cb(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    await _send_my_contests(callback.message, callback.from_user.id)


async def _send_my_contests(message, user_id: int, *, edit: bool = False):
    async with session_scope() as session:
        contests = await cs.list_contests_by_owner(session, user_id)
        rows = []
        for c in contests:
            participants = await cs.participants_count(session, c.id)
            winners = await cs.winners_count(session, c.id)
            rows.append((c, participants, winners))

    kb = InlineKeyboardBuilder()
    if not rows:
        text = (
            "📣 <b>Мои конкурсы</b>\n\n"
            "У тебя пока нет конкурсов.\n\n"
            "Создать конкурс можно для канала, где ты <b>владелец</b> или "
            "<b>администратор</b>, — или вообще без канала. Нажми «➕ Создать конкурс»."
        )
        kb.button(text="➕ Создать конкурс", callback_data="uc_create")
    else:
        active = sum(1 for c, _, _ in rows if cs.compute_status(c) == cs.STATUS_ACTIVE)
        total_parts = sum(p for _, p, _ in rows)
        total_wins = sum(w for _, _, w in rows)
        lines = [
            "📣 <b>Мои конкурсы</b>\n",
            f"📊 Всего: <b>{len(rows)}</b> | 🟢 активных: <b>{active}</b> | "
            f"👥 участников: <b>{total_parts}</b> | 🏆 победителей: <b>{total_wins}</b>\n",
        ]
        for c, participants, winners in rows:
            status = _status_label(c)
            left = _time_left(c)
            lines.append(
                f"• <b>{c.title}</b> <i>(ID {c.id})</i>\n"
                f"   {status} | 🏁 {left}\n"
                f"   👥 {participants} | 🏆 {winners}/{c.winners_count}"
            )
        text = "\n".join(lines)
        for c, _, _ in rows[:12]:
            kb.button(text=f"📊 {c.title[:32]}", callback_data=f"uc_view_{c.id}")
        kb.button(text="➕ Создать конкурс", callback_data="uc_create")
    kb.adjust(1)
    markup = kb.as_markup()
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


# ===================== КАРТОЧКА КОНКУРСА ВЛАДЕЛЬЦА =====================

@router.callback_query(F.data.startswith("uc_view_"))
async def on_owner_view_contest(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("uc_view_", ""))
    await _render_owner_contest(callback, contest_id)


async def _render_owner_contest(callback: types.CallbackQuery, contest_id: int):
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest or contest.owner_id != callback.from_user.id:
            try:
                await callback.answer("Конкурс не найден", show_alert=True)
            except Exception:
                pass
            return
        participants = await cs.participants_count(session, contest_id)
        winners = await cs.winners_count(session, contest_id)
        channels = await cs.get_contest_channels(session, contest_id)
        latest_winners = await ds.get_latest_winners(session, contest_id)
        reserves = await ds.get_latest_reserves(session, contest_id)
        settings = cset.read_settings(contest)
    try:
        await callback.answer()
    except Exception:
        pass

    status = cs.compute_status(contest)
    status_label = _status_label(contest)
    ends = contest.ends_at.strftime("%d.%m.%Y %H:%M") if contest.ends_at else "—"
    winners_goal = max(1, contest.winners_count)
    progress = _progress_bar(winners, winners_goal)

    lines = [
        f"🎯 <b>{contest.title}</b> <i>(ID {contest.id})</i>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Статус: {status_label}",
        f"🏁 Окончание: {ends}",
    ]
    if status in (cs.STATUS_ACTIVE, cs.STATUS_SCHEDULED):
        lines.append(f"⏳ Осталось: <b>{_time_left(contest)}</b>")
    if contest.prize:
        lines.append(f"🎁 Приз: <b>{contest.prize}</b>")
    lines.append(f"👥 Участников: <b>{participants}</b>")
    lines.append(f"🏆 Победителей: <b>{winners}</b>/{winners_goal}  {progress}")
    if channels:
        lines.append("📢 Каналы: " + ", ".join(
            ch.title or ch.username or str(ch.chat_id) for ch in channels
        ))
    else:
        lines.append("🌍 Без обязательной подписки (участвовать могут все)")

    lines.append(
        f"🎟 Билетов за реферала: <b>{settings['tickets_per_ref']}</b> · "
        f"🔄 Запасных: {settings['reserve_winners']}"
    )
    if latest_winners:
        lines.append("\n🥇 <b>Победители:</b>")
        for w, player in latest_winners[:5]:
            name = (player.custom_nickname or player.first_name) if player else f"ID {w.user_id}"
            lines.append(f"   {w.place}. {name}")
    if reserves:
        lines.append("🔄 <b>Запасные:</b> " + ", ".join(
            (p.custom_nickname or p.first_name) if p else f"ID {w.user_id}" for w, p in reserves[:5]
        ))

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data=f"uc_stats_{contest.id}")
    kb.button(text="👥 Участники", callback_data=f"uc_parts_{contest.id}")
    if status in (cs.STATUS_ACTIVE, cs.STATUS_SCHEDULED):
        kb.button(text="🏁 Завершить", callback_data=f"uc_finish_{contest.id}")
        kb.button(text="❌ Отменить", callback_data=f"uc_cancel_{contest.id}")
    if status == cs.STATUS_FINISHED and not contest.draw_done:
        kb.button(text="🎲 Провести розыгрыш", callback_data=f"uc_draw_{contest.id}")
    if status in (cs.STATUS_FINISHED, cs.STATUS_CANCELLED):
        kb.button(text="🏆 Итоги", callback_data=f"uc_results_{contest.id}")
    if settings.get("sources_report"):
        kb.button(text="📈 Источники", callback_data=f"uc_sources_{contest.id}")
    kb.button(text="📤 Экспорт CSV", callback_data=f"uc_export_{contest.id}")
    kb.button(text="🧾 Проверить честность", callback_data=f"uc_verify_{contest.id}")
    kb.button(text="◀️ К моим конкурсам", callback_data="uc_my_contests")
    kb.adjust(2, 2, 1, 1, 1, 1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("uc_parts_"))
async def on_owner_participants(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("uc_parts_", ""))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest or contest.owner_id != callback.from_user.id:
            await callback.answer("Конкурс не найден", show_alert=True)
            return
        entries = await cs.list_entries(session, contest_id, active_only=False)
        settings = cset.read_settings(contest)
        rows = []
        for e in entries:
            rows.append((e, await session.get(Player, e.user_id)))
    try:
        await callback.answer()
    except Exception:
        pass
    if not rows:
        text = "👥 Участников пока нет. Поделись ссылкой на конкурс, чтобы привлечь людей!"
    else:
        active = sum(1 for e, _ in rows if e.status == "active")
        left = len(rows) - active
        lines = [
            f"👥 <b>Участники ({len(rows)})</b>",
            f"🟢 активных: {active} | 🚪 вышли: {left}\n",
        ]
        show_tickets = bool(settings.get("show_tickets"))
        for i, (e, p) in enumerate(rows[:50], start=1):
            name = (p.custom_nickname or p.first_name) if p else f"ID {e.user_id}"
            uname = f" (@{p.username})" if p and p.username else ""
            mark = "🟢" if e.status == "active" else "🚪"
            tickets = f" — 🎟 {int(e.tickets or 0)}" if show_tickets else ""
            lines.append(f"{i}. {mark} {name}{uname}{tickets}")
        if len(rows) > 50:
            lines.append(f"\n…и ещё {len(rows) - 50}")
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=f"uc_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


# ===================== СТАТИСТИКА =====================

@router.callback_query(F.data.startswith("uc_stats_"))
async def on_owner_stats(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("uc_stats_", ""))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest or contest.owner_id != callback.from_user.id:
            try:
                await callback.answer("Конкурс не найден", show_alert=True)
            except Exception:
                pass
            return
        stats = await cs.contest_statistics(session, contest_id)
        logs = await ds.get_draw_history(session, contest_id)
    try:
        await callback.answer()
    except Exception:
        pass

    timeline = stats["timeline"]
    by_hour = timeline["by_hour"]
    max_hour = max(by_hour.values()) if by_hour else 0
    peak_hour = timeline.get("peak_hour")

    active = stats["participants_active"]
    lines = [
        f"📊 <b>Статистика «{stats['title']}»</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Статус: {stats['status_label']}",
        "",
        f"👥 Активных участников: <b>{active}</b>",
        f"🚪 Отменили участие: <b>{stats['participants_left']}</b>",
        f"👤 Всего записей: <b>{stats['participants_total']}</b>",
        f"🔗 Пришли по приглашениям: <b>{stats['referred_participants']}</b>",
        f"🏆 Победителей: <b>{stats['winners']}</b>",
    ]
    if active:
        lines.append(
            f"🎯 Шанс победы (в среднем): ~1 к "
            f"{max(1, round(active / max(1, stats['winners'])))}"
        )
    if stats["duration_days"] is not None:
        lines.append(f"⏳ Длительность: {stats['duration_days']:.1f} дн.")

    if peak_hour is not None:
        lines.append(
            f"\n⏰ <b>Пик регистраций:</b> {peak_hour:02d}:00 "
            f"({timeline['peak_hour_value']} участн.)"
        )
        lines.append("<b>Активность по часам (UTC):</b>")
        for h in range(24):
            v = by_hour.get(h, 0)
            if v > 0 or h % 3 == 0:
                lines.append(f"<code>{h:02d}:00</code> {_hour_bar(v, max_hour)} {v}")

    by_weekday = timeline["by_weekday"]
    if any(by_weekday.values()):
        labels = cs.weekday_labels()
        max_wd = max(by_weekday.values())
        lines.append("\n📅 <b>По дням недели:</b>")
        for d in range(7):
            lines.append(f"<code>{labels[d]}</code> {_hour_bar(by_weekday.get(d, 0), max_wd, 8)} {by_weekday.get(d, 0)}")

    by_day = timeline.get("by_day") or {}
    if by_day:
        recent = list(by_day.items())[-7:]
        max_day = max(by_day.values())
        lines.append("\n🗓 <b>Последние дни:</b>")
        for day, v in recent:
            lines.append(f"<code>{day}</code> {_hour_bar(v, max_day, 8)} {v}")

    if logs:
        lines.append("\n🎲 <b>Розыгрыши:</b>")
        for lg in logs:
            reason = f" — {lg.reason}" if lg.reason else ""
            lines.append(
                f"• Раунд {lg.round}: {lg.winners_count} победителей "
                f"(алг. {lg.algorithm_version}){reason}"
            )

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"uc_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("uc_results_"))
async def on_owner_results(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("uc_results_", ""))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest or contest.owner_id != callback.from_user.id:
            try:
                await callback.answer("Конкурс не найден", show_alert=True)
            except Exception:
                pass
            return
        winners = await ds.get_latest_winners(session, contest_id)
        participants = await cs.participants_count(session, contest_id)
    try:
        await callback.answer()
    except Exception:
        pass
    lines = [f"🏆 <b>Итоги «{contest.title}»</b>\n"]
    if winners:
        lines.append(f"👥 Участвовало: {participants} | 🏆 Победителей: {len(winners)}\n")
        for w, player in winners:
            name = (player.custom_nickname or player.first_name) if player else f"ID {w.user_id}"
            uname = f" (@{player.username})" if player and player.username else ""
            prize = f" — 🎁 {w.prize}" if w.prize else ""
            lines.append(f"{w.place}. <b>{name}</b>{uname}{prize}")
    else:
        lines.append("Победители ещё не определены. Нажми «🎲 Провести розыгрыш».")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"uc_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


# ===================== ДЕЙСТВИЯ ВЛАДЕЛЬЦА =====================

async def _owner_guard(callback: types.CallbackQuery, contest_id: int) -> bool:
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        return bool(contest and contest.owner_id == callback.from_user.id)


@router.callback_query(F.data.startswith("uc_finish_"))
async def on_owner_finish(callback: types.CallbackQuery, bot: Bot):
    contest_id = int(callback.data.replace("uc_finish_", ""))
    if not await _owner_guard(callback, contest_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with session_scope() as session:
        contest = await cs.update_contest(session, contest_id, status=cs.STATUS_FINISHED)
        title = contest.title
        await log_action(session, actor_id=callback.from_user.id, action="contest_finish",
                         target_type="contest", target_id=contest_id)
    try:
        await callback.answer("Конкурс завершён")
    except Exception:
        pass
    async with session_scope() as session:
        try:
            await notify.notify_contest_participants(
                session, bot, contest_id=contest_id, event_type="contest_finished",
                event_key_suffix="owner_finished",
                text_builder=f"🏁 Конкурс «{title}» завершён. Скоро объявим победителей!",
            )
        except Exception:
            pass
    await _render_owner_contest(callback, contest_id)


@router.callback_query(F.data.startswith("uc_cancel_"))
async def on_owner_cancel(callback: types.CallbackQuery, bot: Bot):
    contest_id = int(callback.data.replace("uc_cancel_", ""))
    if not await _owner_guard(callback, contest_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with session_scope() as session:
        contest = await cs.update_contest(session, contest_id, status=cs.STATUS_CANCELLED)
        title = contest.title
        await log_action(session, actor_id=callback.from_user.id, action="contest_cancel",
                         target_type="contest", target_id=contest_id)
    try:
        await callback.answer("Конкурс отменён")
    except Exception:
        pass
    async with session_scope() as session:
        try:
            await notify.notify_contest_participants(
                session, bot, contest_id=contest_id, event_type="contest_cancelled",
                event_key_suffix="owner_cancelled",
                text_builder=f"❌ Конкурс «{title}» отменён организатором.",
            )
        except Exception:
            pass
    await _render_owner_contest(callback, contest_id)


@router.callback_query(F.data.startswith("uc_draw_"))
async def on_owner_draw(callback: types.CallbackQuery, bot: Bot):
    contest_id = int(callback.data.replace("uc_draw_", ""))
    if not await _owner_guard(callback, contest_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with session_scope() as session:
        try:
            result = await ds.run_draw(session, contest_id=contest_id, executed_by=callback.from_user.id)
        except ds.DrawError as e:
            try:
                await callback.answer(str(e), show_alert=True)
            except Exception:
                pass
            return
        await log_action(session, actor_id=callback.from_user.id, action="contest_draw",
                         target_type="contest", target_id=contest_id,
                         details={"round": result.round, "winners": result.winners})
        contest = await cs.get_contest(session, contest_id)
        title = contest.title if contest else str(contest_id)
        prize = contest.prize if contest else None
    try:
        await callback.answer("Розыгрыш проведён")
    except Exception:
        pass
    async with session_scope() as session:
        try:
            await notify.notify_winners_selected(
                session, bot, contest_id=contest_id, title=title, winner_ids=result.winners, prize=prize
            )
            if result.reserves:
                await notify.notify_reserve_winners(
                    session, bot, contest_id=contest_id, title=title,
                    reserve_ids=result.reserves, prize=prize,
                )
        except Exception:
            pass
    reserves_line = f" Запасных: {len(result.reserves)}." if result.reserves else ""
    await callback.message.answer(
        f"🎲 Розыгрыш конкурса «{title}» завершён!\n"
        f"Выбрано {len(result.winners)} победителей из {len(result.participants)} участников.{reserves_line}"
    )
    await _render_owner_contest(callback, contest_id)


@router.callback_query(F.data.startswith("uc_sources_"))
async def on_owner_sources(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("uc_sources_", ""))
    if not await _owner_guard(callback, contest_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        report = await cs.sources_report(session, contest_id)
    try:
        await callback.answer()
    except Exception:
        pass
    if not report:
        text = "📈 Пока никто не привёл друзей, выполнивших условия."
    else:
        lines = [f"📈 <b>Источники приглашений «{contest.title}»</b>\n"]
        for i, row in enumerate(report, start=1):
            uname = f" (@{row['username']})" if row.get("username") else ""
            lines.append(f"{i}. <b>{row['name']}</b>{uname} — 👥 {row['invited']} (🎟 {row['tickets_awarded']})")
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"uc_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("uc_export_"))
async def on_owner_export(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("uc_export_", ""))
    if not await _owner_guard(callback, contest_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    from app.handlers.randomizer_admin_handlers import _build_participants_csv

    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        csv_text = await _build_participants_csv(session, contest)
    try:
        await callback.answer("Готовлю файл…")
    except Exception:
        pass
    from aiogram.types import BufferedInputFile

    document = BufferedInputFile(csv_text.encode("utf-8-sig"), filename=f"contest_{contest_id}_participants.csv")
    await callback.message.answer_document(
        document, caption=f"📤 Экспорт участников конкурса «{contest.title}»"
    )


@router.callback_query(F.data.startswith("uc_verify_"))
async def on_owner_verify(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("uc_verify_", ""))
    if not await _owner_guard(callback, contest_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with session_scope() as session:
        logs = await ds.get_draw_history(session, contest_id)
    try:
        await callback.answer()
    except Exception:
        pass
    if not logs:
        try:
            await callback.message.edit_text(
                "🧾 Розыгрышей ещё не было — проверять нечего.",
                reply_markup=InlineKeyboardBuilder()
                .button(text="◀️ К конкурсу", callback_data=f"uc_view_{contest_id}").as_markup(),
            )
        except Exception:
            pass
        return
    lines = ["🧾 <b>Проверка честности розыгрышей</b>\n"]
    for log in logs:
        reproduced = ds.verify_draw(log)
        stored = [int(x) for x in (log.winners or [])]
        ok = reproduced == stored
        lines.append(
            f"• Раунд {log.round} (алг. {log.algorithm_version}): "
            f"{'✅ подтверждён' if ok else '⚠️ расхождение'}\n"
            f"   seed: <code>{log.seed}</code>\n"
            f"   соль: <code>{log.salt or '—'}</code>\n"
            f"   SHA-256: <code>{log.participants_hash or '—'}</code>\n"
            f"   победители: {', '.join(str(x) for x in stored) or '—'}"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"uc_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


# ===================== МАСТЕР СОЗДАНИЯ КОНКУРСА =====================

class UserContestWizard(StatesGroup):
    channel = State()
    title = State()
    description = State()
    image = State()
    prize = State()
    winners = State()
    ends = State()
    conditions = State()
    settings = State()
    settings_value = State()
    confirm = State()


async def _start_create(event, state: FSMContext, is_callback: bool):
    await state.clear()
    await state.set_state(UserContestWizard.channel)
    text = (
        "➕ <b>Создание конкурса</b>\n\n"
        "Шаг 1/8. Укажи канал, для которого создаётся конкурс: отправь "
        "@username канала или ссылку вида https://t.me/канал.\n\n"
        "Ты должен быть <b>владельцем</b> или <b>администратором</b> этого канала, "
        "а бот — добавлен в канал администратором, чтобы проверять подписку "
        "участников.\n\n"
        "💡 <b>Нет канала?</b> Нажми «⏭ Пропустить» — конкурс создастся без "
        "обязательной подписки, участвовать смогут все."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить (без канала)", callback_data="uc_wiz_skip")
    kb.button(text="❌ Отменить", callback_data="uc_wiz_cancel")
    kb.adjust(1)
    markup = kb.as_markup()
    if is_callback:
        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("createcontest"))
@router.message(F.text.casefold().in_(TRIGGERS_CREATE))
async def cmd_create_contest(message: types.Message, state: FSMContext):
    if not message.from_user:
        return
    await _start_create(message, state, is_callback=False)


@router.callback_query(F.data == "uc_create")
async def on_create_contest_cb(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    await _start_create(callback, state, is_callback=True)


@router.callback_query(F.data == "uc_wiz_cancel")
async def on_wizard_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.answer("Отменено")
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К моим конкурсам", callback_data="uc_my_contests")
    try:
        await callback.message.edit_text("❌ Создание конкурса отменено.", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer("❌ Создание конкурса отменено.", reply_markup=kb.as_markup())


@router.callback_query(F.data == "uc_wiz_skip")
async def on_wizard_skip(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer("Пропущено")
    except Exception:
        pass
    current = await state.get_state()
    # Шаг 1 (канал) не входит в _UC_FIELDS — обрабатываем пропуск отдельно.
    if current == UserContestWizard.channel.state:
        await state.update_data(channel_id=None, channel_title=None)
        await state.set_state(UserContestWizard.title)
        await callback.message.answer(
            "Ок, конкурс будет без обязательной подписки на канал. 🌍\n\n"
            "Шаг 2/8. Введите название конкурса:",
            reply_markup=_skip_kb(),
            parse_mode="HTML",
        )
        return
    await _wizard_advance(callback.message, state, None)


@router.message(UserContestWizard.channel)
async def uc_channel(message: types.Message, state: FSMContext, bot: Bot):
    token = (message.text or "").strip()
    if not token:
        await message.answer("❌ Отправь @username канала или ссылку (или нажми «⏭ Пропустить»).")
        return
    if token.lower() in ("-", "нет", "пропустить", "skip", "—", "без канала"):
        await state.update_data(channel_id=None, channel_title=None)
        await state.set_state(UserContestWizard.title)
        await message.answer(
            "Ок, конкурс будет без обязательной подписки на канал. 🌍\n\n"
            "Шаг 2/8. Введите название конкурса:",
            reply_markup=_skip_kb(),
            parse_mode="HTML",
        )
        return

    channel = None
    async with session_scope() as session:
        channel = await chs.ensure_channel(session, bot, token, added_by=message.from_user.id)
        if channel is None:
            await message.answer(
                "❌ Не удалось найти канал. Проверь @username/ссылку и убедись, что бот добавлен в канал.\n\n"
                "💡 Можно нажать «⏭ Пропустить» и создать конкурс без канала."
            )
            return
        if channel.chat_id is None:
            await message.answer(
                "❌ У канала нет chat_id — добавь бота в канал и попробуй снова, "
                "или нажми «⏭ Пропустить»."
            )
            return
        is_admin = await chs.is_channel_admin(bot, channel.chat_id, message.from_user.id)
        if not is_admin:
            await message.answer(
                "⛔ Ты не являешься владельцем или администратором этого канала.\n"
                "Укажи свой канал или нажми «⏭ Пропустить», чтобы создать конкурс без канала."
            )
            return
        channel.owner_id = message.from_user.id
        await session.flush()
        channel_id = channel.id
        channel_title = channel.title or channel.username or str(channel.chat_id)
        rights_warn = await chs.check_bot_rights(bot, channel.chat_id)

    await state.update_data(channel_id=channel_id, channel_title=channel_title)
    warn = f"\n\n{rights_warn}" if rights_warn else ""
    await state.set_state(UserContestWizard.title)
    await message.answer(
        f"✅ Канал «{channel_title}» подтверждён — ты его администратор.{warn}\n\n"
        "Шаг 2/8. Введите название конкурса:",
        reply_markup=_skip_kb(),
        parse_mode="HTML",
    )


@router.message(UserContestWizard.title)
async def uc_title(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


@router.message(UserContestWizard.description)
async def uc_description(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


@router.message(UserContestWizard.image)
async def uc_image(message: types.Message, state: FSMContext):
    value = None
    if message.photo:
        value = message.photo[-1].file_id
    elif message.text:
        value = message.text.strip()
    await _wizard_advance(message, state, value)


@router.message(UserContestWizard.prize)
async def uc_prize(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


@router.message(UserContestWizard.winners)
async def uc_winners(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ Введите целое число победителей (не меньше 1).")
        return
    await _wizard_advance(message, state, int(text))


@router.message(UserContestWizard.ends)
async def uc_ends(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    skip = raw.lower() in ("-", "нет", "пропустить", "skip", "—")
    dt = _parse_dt(raw)
    if dt is None and not skip:
        await message.answer(
            "❌ Неверный формат даты. Пример: <code>31.12.2026 20:00</code> или отправьте «-».",
            parse_mode="HTML",
        )
        return
    await _wizard_advance(message, state, dt)


@router.message(UserContestWizard.conditions)
async def uc_conditions(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


_UC_FIELDS = [
    ("title", "title", UserContestWizard.description, "Шаг 3/8. Введите описание конкурса:"),
    ("description", "description", UserContestWizard.image, "Шаг 4/8. Отправьте изображение приза (фото или ссылку, «-» чтобы пропустить):"),
    ("image", "image_url", UserContestWizard.prize, "Шаг 5/8. Введите приз (что разыгрывается):"),
    ("prize", "prize", UserContestWizard.winners, "Шаг 6/8. Сколько победителей? (число)"),
    ("winners", "winners_count", UserContestWizard.ends, "Шаг 7/8. Дата окончания (ДД.ММ.ГГГГ ЧЧ:ММ) или «-»:"),
    ("ends", "ends_at", UserContestWizard.conditions, "Шаг 8/8. Дополнительные условия участия, или «-»:"),
    ("conditions", "extra_conditions", UserContestWizard.settings, None),
]


async def _wizard_advance(message: types.Message, state: FSMContext, value):
    current = await state.get_state()
    entry = None
    for item in _UC_FIELDS:
        if current == f"UserContestWizard:{item[0]}":
            entry = item
            break
    if entry is None:
        return
    _, data_key, next_state, next_prompt = entry

    if isinstance(value, str) and value.strip().lower() in ("-", "нет", "пропустить", "skip", "—"):
        value = None

    await state.update_data(**{data_key: value})

    if next_state is None:
        data = await state.get_data()
        await state.set_state(UserContestWizard.confirm)
        await _show_preview(message, data)
        return

    if next_state == UserContestWizard.settings:
        data = await state.get_data()
        settings = cset.normalize_settings(data.get("settings"))
        await state.update_data(settings=settings)
        await state.set_state(UserContestWizard.settings)
        await _render_settings(message, settings)
        return

    await state.set_state(next_state)
    await message.answer(next_prompt, reply_markup=_skip_kb(), parse_mode="HTML")


def _settings_kb(settings: dict):
    kb = InlineKeyboardBuilder()
    for key in cset.TOGGLE_ORDER:
        label = cset.TOGGLE_LABELS.get(key, key)
        mark = "✅" if settings.get(key) else "⬜️"
        kb.button(text=f"{mark} {label}", callback_data=f"uc_set_{key}")
    kb.button(
        text=f"🎟 Билетов за реферала: {settings.get('tickets_per_ref', 1)}",
        callback_data="uc_set_tickets_per_ref",
    )
    kb.button(
        text=f"🔄 Запасных победителей: {settings.get('reserve_winners', 0)}",
        callback_data="uc_set_reserve_winners",
    )
    kb.button(text="🔑 Настроить кодовые слова", callback_data="uc_set_code_words_list")
    kb.button(text="🎯 Настроить задание спонсора", callback_data="uc_set_sponsor_task")
    kb.button(text="🏅 Размер рейтинга", callback_data="uc_set_leaderboard_size")
    kb.button(text="✅ Готово", callback_data="uc_set_done")
    kb.adjust(1)
    return kb.as_markup()


async def _render_settings(message, settings: dict, *, edit: bool = False):
    words = settings.get("code_words_list") or []
    lines = [
        "⚙️ <b>Настройки конкурса</b>\n",
        "Включай механики кнопками. Текущее состояние:",
        f"• Кодовые слова: {'✅' if settings['code_words'] else '⬜️'} ({len(words)} шт.)",
        f"• Задания спонсоров: {'✅' if settings['sponsor_tasks'] else '⬜️'}",
        f"• Билетов за 1 реферала: <b>{settings['tickets_per_ref']}</b>",
        f"• Показывать участников: {'✅' if settings['show_participants'] else '⬜️'}",
        f"• Счётчик билетов: {'✅' if settings['show_tickets'] else '⬜️'}",
        f"• Кнопка «Поделиться»: {'✅' if settings['share_button'] else '⬜️'}",
        f"• Рейтинг приглашающих: {'✅' if settings['leaderboard'] else '⬜️'} (топ-{settings['leaderboard_size']})",
        f"• Запасных победителей: <b>{settings['reserve_winners']}</b>",
        "",
        "Когда всё готово — нажми «✅ Готово».",
    ]
    markup = _settings_kb(settings)
    if edit:
        try:
            await message.edit_text("\n".join(lines), reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer("\n".join(lines), reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("uc_set_"))
async def on_uc_settings_toggle(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.replace("uc_set_", "")
    data = await state.get_data()
    settings = cset.normalize_settings(data.get("settings"))
    try:
        await callback.answer()
    except Exception:
        pass

    if action == "done":
        await state.update_data(settings=settings)
        await state.set_state(UserContestWizard.confirm)
        await _show_preview(callback.message, await state.get_data())
        return

    if action in ("tickets_per_ref", "reserve_winners", "leaderboard_size"):
        prompts = {
            "tickets_per_ref": "Сколько билетов за 1 приведённого друга? (число)",
            "reserve_winners": "Сколько запасных победителей? (число, 0 — без запасных)",
            "leaderboard_size": "Сколько мест в рейтинге приглашающих? (число)",
        }
        await state.update_data(settings=settings, settings_field=action)
        await state.set_state(UserContestWizard.settings_value)
        await callback.message.answer(prompts[action])
        return

    if action == "code_words_list":
        await state.update_data(settings=settings, settings_field="code_words_list")
        await state.set_state(UserContestWizard.settings_value)
        await callback.message.answer(
            "Отправь кодовые слова в формате <code>слово:билеты</code> через запятую.\n"
            "Пример: <code>FABLE:3, ПРИЗ:5</code>. «-» — очистить.",
            parse_mode="HTML",
        )
        return

    if action == "sponsor_task":
        await state.update_data(settings=settings, settings_field="sponsor_task")
        await state.set_state(UserContestWizard.settings_value)
        await callback.message.answer(
            "Отправь задание спонсора в формате <code>текст|билеты</code>. «-» — очистить.",
            parse_mode="HTML",
        )
        return

    if action in settings and isinstance(settings[action], bool):
        settings = cset.toggle(settings, action)
        await state.update_data(settings=settings)
    await _render_settings(callback.message, settings, edit=True)


@router.message(UserContestWizard.settings_value)
async def uc_settings_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    settings = cset.normalize_settings(data.get("settings"))
    field = data.get("settings_field")
    raw = (message.text or "").strip()
    cleared = raw.lower() in ("-", "нет", "очистить", "—", "")

    if field == "tickets_per_ref":
        if not raw.isdigit() or int(raw) < 1:
            await message.answer("❌ Введи целое число не меньше 1.")
            return
        settings["tickets_per_ref"] = int(raw)
    elif field == "reserve_winners":
        if not raw.isdigit():
            await message.answer("❌ Введи целое число (0 — без запасных).")
            return
        settings["reserve_winners"] = int(raw)
    elif field == "leaderboard_size":
        if not raw.isdigit() or int(raw) < 1:
            await message.answer("❌ Введи целое число не меньше 1.")
            return
        settings["leaderboard_size"] = int(raw)
    elif field == "code_words_list":
        words = []
        if not cleared:
            for part in re.split(r"[,\n]+", raw):
                part = part.strip()
                if not part:
                    continue
                if ":" in part:
                    word, _, tcount = part.partition(":")
                    word = word.strip()
                    tickets_n = int(tcount) if tcount.strip().isdigit() else 1
                else:
                    word, tickets_n = part, 1
                if word:
                    words.append({"word": word, "tickets": max(1, tickets_n)})
        settings["code_words_list"] = words
        if words:
            settings["code_words"] = True
    elif field == "sponsor_task":
        if cleared:
            settings["sponsor_task_text"] = ""
        else:
            text_part, _, tickets_part = raw.partition("|")
            settings["sponsor_task_text"] = text_part.strip()
            settings["sponsor_task_tickets"] = int(tickets_part) if tickets_part.strip().isdigit() else 1
            settings["sponsor_tasks"] = True

    settings = cset.normalize_settings(settings)
    await state.update_data(settings=settings, settings_field=None)
    await state.set_state(UserContestWizard.settings)
    await message.answer("✅ Сохранено.")
    await _render_settings(message, settings)


async def _show_preview(message: types.Message, data: dict):
    ends = data.get("ends_at")
    ends_s = ends.strftime("%d.%m.%Y %H:%M") if isinstance(ends, datetime) else "без ограничения"
    channel = data.get("channel_title")
    lines = [
        "📋 <b>Предпросмотр конкурса</b>\n",
        f"📢 Канал: <b>{channel or 'без канала (участвовать смогут все)'}</b>",
        f"📛 Название: <b>{data.get('title') or '—'}</b>",
        f"📝 Описание: {data.get('description') or '—'}",
        f"🖼 Изображение: {'есть ✅' if data.get('image_url') else '—'}",
        f"🎁 Приз: {data.get('prize') or '—'}",
        f"🏆 Победителей: {data.get('winners_count') or 1}",
        f"🏁 Окончание: {ends_s}",
        f"📌 Условия: {data.get('extra_conditions') or '—'}",
        "",
    ]
    settings = cset.normalize_settings(data.get("settings"))
    lines += [
        "⚙️ <b>Настройки:</b>",
        f"• Кодовые слова: {'✅' if settings['code_words'] else '⬜️'} ({len(settings['code_words_list'])} шт.)",
        f"• Задания спонсоров: {'✅' if settings['sponsor_tasks'] else '⬜️'}",
        f"• Билетов за 1 реферала: {settings['tickets_per_ref']}",
        f"• Показывать участников: {'✅' if settings['show_participants'] else '⬜️'}",
        f"• Счётчик билетов: {'✅' if settings['show_tickets'] else '⬜️'}",
        f"• Кнопка «Поделиться»: {'✅' if settings['share_button'] else '⬜️'}",
        f"• Рейтинг приглашающих: {'✅' if settings['leaderboard'] else '⬜️'} (топ-{settings['leaderboard_size']})",
        f"• Запасных победителей: {settings['reserve_winners']}",
        "",
    ]
    if channel:
        lines.append("ℹ️ Участники должны будут подписаться на твой канал.")
        lines.append("❗️ Как владелец канала ты <b>не участвуешь</b> в розыгрыше своего конкурса.")
    else:
        lines.append("ℹ️ Обязательной подписки нет — участвовать смогут все.")
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Опубликовать", callback_data="uc_wiz_publish")
    kb.button(text="✏️ Начать заново", callback_data="uc_wiz_restart")
    kb.button(text="❌ Отменить", callback_data="uc_wiz_cancel")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "uc_wiz_restart")
async def on_wizard_restart(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    await _start_create(callback, state, is_callback=True)


@router.callback_query(F.data == "uc_wiz_publish")
async def on_wizard_publish(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    try:
        await callback.answer("Публикую...")
    except Exception:
        pass
    data = await state.get_data()
    channel_id = data.get("channel_id")
    async with session_scope() as session:
        ends = data.get("ends_at")
        status = cs.STATUS_ACTIVE
        contest = await cs.create_contest(
            session,
            title=data.get("title") or "Без названия",
            description=data.get("description") or "",
            image_url=data.get("image_url"),
            prize=data.get("prize"),
            winners_count=data.get("winners_count") or 1,
            ends_at=ends,
            extra_conditions=data.get("extra_conditions"),
            status=status,
            created_by=callback.from_user.id,
            owner_id=callback.from_user.id,
            source_channel_id=channel_id,
            settings=data.get("settings"),
        )
        # Обязательная подписка на канал владельца (если канал указан).
        if channel_id:
            await cs.set_contest_channels(session, contest.id, [channel_id])
        await log_action(
            session, actor_id=callback.from_user.id, action="contest_create_user",
            target_type="contest", target_id=contest.id,
            details={"title": contest.title, "channel_id": channel_id},
        )
        contest_id = contest.id
        title = contest.title

    await state.clear()
    # Пытаемся определить username бота для красивой ссылки-приглашения.
    async with session_scope() as session:
        bot_username = await rset.get_bot_username(session)
    link_line = ""
    if bot_username:
        link_line = (
            f"\n\n🔗 <b>Ссылка на конкурс</b> (делись с участниками):\n"
            f"<code>https://t.me/{bot_username}?start=c_{contest_id}</code>"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Мои конкурсы", callback_data="uc_my_contests")
    kb.button(text="👁 Открыть конкурс", callback_data=f"rz_view_{contest_id}")
    kb.adjust(1)
    await callback.message.answer(
        f"🎉 Конкурс «{title}» опубликован! (ID: <code>{contest_id}</code>)\n\n"
        "Участники могут присоединяться через раздел «🎁 Конкурсы». "
        "Статистику смотри в «📣 Мои конкурсы» → «📊 Статистика»." + link_line,
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
