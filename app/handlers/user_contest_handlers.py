"""Пользовательские конкурсы Fable Randomizer.

Любой пользователь, который является владельцем или администратором Telegram-канала,
может создать конкурс для этого канала. Права проверяются через Telegram Bot API
(``getChatMember``), поэтому подделать владение каналом нельзя.

Правила:
- владелец конкурса (владелец канала) **не может выиграть** в своём конкурсе;
- обязательная подписка на канал владельца проверяется автоматически;
- владелец видит полную статистику: участники, отмены, конверсия, розыгрыши и
  распределение регистраций по часам/дням (пиковые периоды);
- владелец может завершить конкурс, провести розыгрыш и отменить его.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Router, types, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import ContestEntry, Player
from app.db.session import session_scope
from app.services import channel_service as chs
from app.services import contest_service as cs
from app.services import draw_service as ds
from app.services import notifications_service as notify
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
            "<b>администратор</b> — нажми «➕ Создать конкурс»."
        )
        kb.button(text="➕ Создать конкурс", callback_data="uc_create")
    else:
        lines = ["📣 <b>Мои конкурсы</b>\n"]
        for c, participants, winners in rows:
            lines.append(
                f"• <b>{c.title}</b> (ID <code>{c.id}</code>) — {_status_label(c)}\n"
                f"   👥 {participants} участников | 🏆 победителей: {winners}/{c.winners_count}"
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
    try:
        await callback.answer()
    except Exception:
        pass

    ends = contest.ends_at.strftime("%d.%m.%Y %H:%M") if contest.ends_at else "—"
    lines = [
        f"📊 <b>{contest.title}</b> (ID <code>{contest.id}</code>)",
        f"Статус: {_status_label(contest)} | 🏁 Окончание: {ends}",
        f"👥 Участников: <b>{participants}</b> | 🏆 Победителей: {winners}/{contest.winners_count}",
    ]
    if contest.prize:
        lines.append(f"🎁 Приз: {contest.prize}")
    if channels:
        lines.append("📢 Каналы: " + ", ".join(ch.title or ch.username or str(ch.chat_id) for ch in channels))

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data=f"uc_stats_{contest.id}")
    kb.button(text="👥 Участники", callback_data=f"uc_parts_{contest.id}")
    if cs.compute_status(contest) in (cs.STATUS_ACTIVE, cs.STATUS_SCHEDULED):
        kb.button(text="🏁 Завершить", callback_data=f"uc_finish_{contest.id}")
        kb.button(text="❌ Отменить", callback_data=f"uc_cancel_{contest.id}")
    if cs.compute_status(contest) == cs.STATUS_FINISHED and not contest.draw_done:
        kb.button(text="🎲 Провести розыгрыш", callback_data=f"uc_draw_{contest.id}")
    kb.button(text="◀️ К моим конкурсам", callback_data="uc_my_contests")
    kb.adjust(2, 2, 1)
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
        rows = []
        for e in entries:
            rows.append((e, await session.get(Player, e.user_id)))
    try:
        await callback.answer()
    except Exception:
        pass
    if not rows:
        text = "👥 Участников пока нет."
    else:
        lines = [f"👥 <b>Участники ({len(rows)}):</b>\n"]
        for e, p in rows[:50]:
            name = (p.custom_nickname or p.first_name) if p else f"ID {e.user_id}"
            uname = f" (@{p.username})" if p and p.username else ""
            mark = "🟢" if e.status == "active" else "🚪"
            lines.append(f"{mark} {name}{uname}")
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

    lines = [
        f"📊 <b>Статистика конкурса «{stats['title']}»</b>\n",
        f"Статус: {stats['status_label']}",
        f"👥 Активных участников: <b>{stats['participants_active']}</b>",
        f"🚪 Отменили участие: <b>{stats['participants_left']}</b>",
        f"👤 Всего записей: <b>{stats['participants_total']}</b>",
        f"🔗 Пришли по приглашениям: <b>{stats['referred_participants']}</b>",
        f"🏆 Победителей: <b>{stats['winners']}</b>",
    ]
    if stats["participants_active"]:
        lines.append(f"🎯 Шанс победы (в среднем): ~1 к {max(1, round(stats['participants_active'] / max(1, stats['winners'])))}")
    if stats["duration_days"] is not None:
        lines.append(f"⏳ Длительность: {stats['duration_days']:.1f} дн.")

    if peak_hour is not None:
        lines.append(f"\n⏰ <b>Пик регистраций:</b> {peak_hour:02d}:00 ({timeline['peak_hour_value']} участн.)")
        lines.append("<b>Активность по часам (UTC):</b>")
        for h in range(24):
            v = by_hour.get(h, 0)
            if v > 0 or h % 3 == 0:
                lines.append(f"<code>{h:02d}:00</code> {_hour_bar(v, max_hour)} {v}")

    by_weekday = timeline["by_weekday"]
    if any(by_weekday.values()):
        labels = cs.weekday_labels()
        lines.append("\n📅 <b>По дням недели:</b>")
        lines.append("  ".join(f"{labels[d]}:{by_weekday.get(d, 0)}" for d in range(7)))

    if logs:
        lines.append("\n🎲 <b>Розыгрыши:</b>")
        for lg in logs:
            lines.append(f"• Раунд {lg.round}: {lg.winners_count} победителей (алг. {lg.algorithm_version})")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=f"uc_view_{contest_id}")
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
        except Exception:
            pass
    await callback.message.answer(
        f"🎲 Розыгрыш конкурса «{title}» завершён!\n"
        f"Выбрано {len(result.winners)} победителей из {len(result.participants)} участников."
    )
    await _render_owner_contest(callback, contest_id)


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
    confirm = State()


async def _start_create(event, state: FSMContext, is_callback: bool):
    await state.clear()
    await state.set_state(UserContestWizard.channel)
    text = (
        "➕ <b>Создание конкурса</b>\n\n"
        "Шаг 1/8. Отправь @username канала (или ссылку вида https://t.me/канал), "
        "в котором ты <b>владелец</b> или <b>администратор</b>.\n\n"
        "❗️ Бот должен быть добавлен в этот канал администратором — иначе подписки "
        "участников не будут проверяться."
    )
    markup = _skip_kb()
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
    await _wizard_advance(callback.message, state, None)


@router.message(UserContestWizard.channel)
async def uc_channel(message: types.Message, state: FSMContext, bot: Bot):
    token = (message.text or "").strip()
    if not token:
        await message.answer("❌ Отправь @username канала или ссылку.")
        return

    channel = None
    async with session_scope() as session:
        channel = await chs.ensure_channel(session, bot, token, added_by=message.from_user.id)
        if channel is None:
            await message.answer(
                "❌ Не удалось найти канал. Проверь @username/ссылку и убедись, что бот добавлен в канал."
            )
            return
        if channel.chat_id is None:
            await message.answer("❌ У канала нет chat_id — добавь бота в канал и попробуй снова.")
            return
        is_admin = await chs.is_channel_admin(bot, channel.chat_id, message.from_user.id)
        if not is_admin:
            await message.answer(
                "⛔ Ты не являешься владельцем или администратором этого канала.\n"
                "Создавать конкурс может только владелец канала или его администратор."
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
    ("conditions", "extra_conditions", None, None),
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

    await state.set_state(next_state)
    await message.answer(next_prompt, reply_markup=_skip_kb(), parse_mode="HTML")


async def _show_preview(message: types.Message, data: dict):
    ends = data.get("ends_at")
    ends_s = ends.strftime("%d.%m.%Y %H:%M") if isinstance(ends, datetime) else "—"
    lines = [
        "📋 <b>Предпросмотр конкурса</b>\n",
        f"📢 Канал: <b>{data.get('channel_title') or '—'}</b>",
        f"📛 Название: <b>{data.get('title') or '—'}</b>",
        f"📝 Описание: {data.get('description') or '—'}",
        f"🖼 Изображение: {'есть' if data.get('image_url') else '—'}",
        f"🎁 Приз: {data.get('prize') or '—'}",
        f"🏆 Победителей: {data.get('winners_count') or 1}",
        f"🏁 Окончание: {ends_s}",
        f"📌 Условия: {data.get('extra_conditions') or '—'}",
        "",
        "ℹ️ Участники должны будут подписаться на твой канал.",
        "❗️ Как владелец канала ты <b>не участвуешь</b> в розыгрыше своего конкурса.",
    ]
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
        if not channel_id:
            await callback.message.answer("❌ Канал не подтверждён. Начни заново: «➕ Создать конкурс».")
            await state.clear()
            return
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
        )
        # Обязательная подписка на канал владельца.
        await cs.set_contest_channels(session, contest.id, [channel_id])
        await log_action(
            session, actor_id=callback.from_user.id, action="contest_create_user",
            target_type="contest", target_id=contest.id,
            details={"title": contest.title, "channel_id": channel_id},
        )
        contest_id = contest.id
        title = contest.title
        prize = contest.prize

    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Мои конкурсы", callback_data="uc_my_contests")
    kb.button(text="👁 Открыть конкурс", callback_data=f"rz_view_{contest_id}")
    kb.adjust(1)
    await callback.message.answer(
        f"✅ Конкурс «{title}» опубликован! (ID: <code>{contest_id}</code>)\n\n"
        "Участники могут присоединяться через раздел «🎁 Конкурсы». "
        "Статистику смотри в «📣 Мои конкурсы» → «📊 Статистика».",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
