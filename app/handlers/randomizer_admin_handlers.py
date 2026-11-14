"""Административная панель Fable Randomizer.

Доступ только администраторам (ID из списка администраторов или владелец).
Возможности: статистика, создание/изменение/удаление конкурсов, управление
каналами, управление пользователями, розыгрыши и аудит действий.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Router, types, F, Bot
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.db.session import session_scope
from app.db.models import (
    Channel,
    Contest,
    ContestChannel,
    ContestEntry,
    Player,
    Referral,
    Winner,
)
from app.services import contest_service as cs
from app.services import draw_service as ds
from app.services import notifications_service as notify
from app.services import randomizer_settings as rset
from app.services.audit_service import log_action, get_recent_actions

logger = logging.getLogger("fable.randomizer.admin")
router = Router(name="randomizer_admin_router")

ADMIN_TRIGGERS = {"админ", "!админ", "/admin", "админ панель", "админ-панель", "панель randomizer", "randomizer"}


async def _is_admin(session, user_id: int) -> bool:
    return await rset.is_admin(session, user_id)


async def _deny(message_or_cb):
    text = "⛔ Доступ только для администраторов Fable Randomizer."
    if isinstance(message_or_cb, types.CallbackQuery):
        try:
            await message_or_cb.answer(text, show_alert=True)
        except Exception:
            pass
    else:
        await message_or_cb.answer(text)


# ===================== FSM: СОЗДАНИЕ КОНКУРСА =====================

class ContestWizard(StatesGroup):
    title = State()
    description = State()
    image = State()
    prize = State()
    winners = State()
    starts = State()
    ends = State()
    channels = State()
    conditions = State()
    post = State()
    confirm = State()


def _parse_dt(text: str) -> datetime | None:
    text = (text or "").strip()
    if text in ("-", "нет", "пропустить", "skip", "—"):
        return None
    fmts = (
        "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d.%m.%Y %H:%M:%S",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _wizard_skip_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data="adm_wiz_skip")
    kb.button(text="❌ Отменить", callback_data="adm_wiz_cancel")
    kb.adjust(2)
    return kb.as_markup()


# ===================== ГЛАВНАЯ ПАНЕЛЬ =====================

async def _admin_panel_payload(session):
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    total_users = int(await session.scalar(select(func.count(Player.telegram_id))) or 0)
    new_day = int(await session.scalar(select(func.count(Player.telegram_id)).where(Player.registered_at >= day_ago)) or 0)
    new_week = int(await session.scalar(select(func.count(Player.telegram_id)).where(Player.registered_at >= week_ago)) or 0)
    active_users = int(
        await session.scalar(
            select(func.count(func.distinct(ContestEntry.user_id))).where(ContestEntry.joined_at >= week_ago)
        )
        or 0
    )
    total_contests = int(await session.scalar(select(func.count(Contest.id))) or 0)
    await cs.sync_statuses(session)
    active_contests = int(await session.scalar(select(func.count(Contest.id)).where(Contest.status == cs.STATUS_ACTIVE)) or 0)
    finished_contests = int(await session.scalar(select(func.count(Contest.id)).where(Contest.status == cs.STATUS_FINISHED)) or 0)
    total_participants = int(await session.scalar(select(func.count(ContestEntry.id))) or 0)
    total_winners = int(await session.scalar(select(func.count(Winner.id))) or 0)
    total_referrals = int(await session.scalar(select(func.count(Referral.id))) or 0)

    text = (
        "🛠 <b>АДМИН-ПАНЕЛЬ FABLE RANDOMIZER</b>\n\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"🆕 Новых за день: <b>{new_day}</b>\n"
        f"🆕 Новых за неделю: <b>{new_week}</b>\n"
        f"⚡️ Активных (7 дней): <b>{active_users}</b>\n\n"
        f"🎁 Всего конкурсов: <b>{total_contests}</b>\n"
        f"🟢 Активных конкурсов: <b>{active_contests}</b>\n"
        f"🏁 Завершённых конкурсов: <b>{finished_contests}</b>\n\n"
        f"👥 Всего участников: <b>{total_participants}</b>\n"
        f"🏆 Всего победителей: <b>{total_winners}</b>\n"
        f"🔗 Всего приглашений: <b>{total_referrals}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать конкурс", callback_data="adm_create")
    kb.button(text="📋 Мои конкурсы", callback_data="adm_mycontests")
    kb.button(text="📢 Каналы", callback_data="adm_channels")
    kb.button(text="👤 Пользователи", callback_data="adm_users")
    kb.button(text="📊 Статистика", callback_data="adm_stats")
    kb.button(text="⚙️ Настройки", callback_data="adm_settings")
    kb.button(text="📜 История действий", callback_data="adm_audit")
    kb.adjust(2, 2, 2, 1)
    return text, kb.as_markup()


@router.message(Command("admin"))
@router.message(F.text.casefold().in_(ADMIN_TRIGGERS))
async def cmd_admin_panel(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
        text, markup = await _admin_panel_payload(session)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "adm_panel")
async def on_admin_panel(callback: types.CallbackQuery):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
        text, markup = await _admin_panel_payload(session)
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "adm_stats")
async def on_admin_stats(callback: types.CallbackQuery):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
        text, _ = await _admin_panel_payload(session)
    try:
        await callback.answer()
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="adm_panel")
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
        text, _ = await _admin_panel_payload(session)
    await message.answer(text, parse_mode="HTML")


@router.message(Command("adminhistory"))
async def cmd_admin_history(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
        actions = await get_recent_actions(session, limit=25)
    if not actions:
        await message.answer("📜 История действий пуста.")
        return
    lines = ["📜 <b>История административных действий:</b>\n"]
    for a in actions:
        ts = a.created_at.strftime("%d.%m %H:%M") if a.created_at else ""
        tgt = f" → {a.target_type}:{a.target_id}" if a.target_type else ""
        lines.append(f"• <i>{ts}</i> <b>{a.action}</b> (актор <code>{a.actor_id}</code>){tgt}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "adm_audit")
async def on_admin_audit(callback: types.CallbackQuery):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
        actions = await get_recent_actions(session, limit=25)
    try:
        await callback.answer()
    except Exception:
        pass
    lines = ["📜 <b>История административных действий:</b>\n"] if actions else ["📜 История действий пуста."]
    for a in actions:
        ts = a.created_at.strftime("%d.%m %H:%M") if a.created_at else ""
        tgt = f" → {a.target_type}:{a.target_id}" if a.target_type else ""
        lines.append(f"• <i>{ts}</i> <b>{a.action}</b>{tgt}")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="adm_panel")
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


# ===================== НАСТРОЙКИ =====================

@router.callback_query(F.data == "adm_settings")
async def on_admin_settings(callback: types.CallbackQuery):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
        cfg = await rset.get_settings_dict(session)
    try:
        await callback.answer()
    except Exception:
        pass
    lines = [
        "⚙️ <b>Настройки Randomizer</b>\n",
        f"🆘 Ссылка поддержки: {cfg.get('support_link') or '<i>не задана</i>'}",
        f"🤖 Username бота: {cfg.get('bot_username') or '<i>определяется автоматически</i>'}\n",
        "<b>Уведомления:</b>",
    ]
    kb = InlineKeyboardBuilder()
    for key, label in rset.NOTIFICATION_TYPES.items():
        enabled = bool(cfg.get("notifications", {}).get(key, True))
        lines.append(f"{'🟢' if enabled else '🔴'} {label}")
        kb.button(text=f"{'🟢' if enabled else '🔴'} {label}", callback_data=f"adm_notif_{key}")
    kb.button(text="🆘 Задать ссылку поддержки", callback_data="adm_set_support")
    kb.button(text="◀️ Назад", callback_data="adm_panel")
    kb.adjust(1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_notif_"))
async def on_toggle_notification(callback: types.CallbackQuery):
    key = callback.data.replace("adm_notif_", "")
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
        cfg = await rset.get_settings_dict(session)
        current = bool(cfg.get("notifications", {}).get(key, True))
        await rset.set_notification_enabled(session, key, not current)
    try:
        await callback.answer("Изменено")
    except Exception:
        pass
    await on_admin_settings(callback)


class AdminSettingsWizard(StatesGroup):
    support = State()


@router.callback_query(F.data == "adm_set_support")
async def on_set_support(callback: types.CallbackQuery, state: FSMContext):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
    try:
        await callback.answer()
    except Exception:
        pass
    await state.set_state(AdminSettingsWizard.support)
    await callback.message.answer(
        "🆘 Отправьте ссылку поддержки (например, https://t.me/your_support или @username).",
    )


@router.message(AdminSettingsWizard.support)
async def on_support_value(message: types.Message, state: FSMContext):
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
        await rset.set_support_link(session, (message.text or "").strip())
        await log_action(session, actor_id=message.from_user.id, action="set_support_link", details={"value": message.text})
    await state.clear()
    await message.answer("✅ Ссылка поддержки сохранена.")


# ===================== СОЗДАНИЕ КОНКУРСА (WIZARD) =====================

@router.callback_query(F.data == "adm_create")
async def on_create_contest(callback: types.CallbackQuery, state: FSMContext):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
    try:
        await callback.answer()
    except Exception:
        pass
    await state.clear()
    await state.set_state(ContestWizard.title)
    await callback.message.answer(
        "➕ <b>Создание конкурса</b>\n\nШаг 1/10. Введите название конкурса:",
        reply_markup=_wizard_skip_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "adm_wiz_cancel")
async def on_wizard_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.answer("Отменено")
    except Exception:
        pass
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В админ-панель", callback_data="adm_panel")
    try:
        await callback.message.edit_text("❌ Создание конкурса отменено.", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer("❌ Создание конкурса отменено.", reply_markup=kb.as_markup())


@router.callback_query(F.data == "adm_wiz_skip")
async def on_wizard_skip(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Пропущено")
    await _wizard_advance(callback.message, state, None, is_callback=True)


@router.message(ContestWizard.title)
async def wiz_title(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


@router.message(ContestWizard.description)
async def wiz_description(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


@router.message(ContestWizard.image)
async def wiz_image(message: types.Message, state: FSMContext):
    value = None
    if message.photo:
        value = message.photo[-1].file_id
    elif message.text:
        value = message.text.strip()
    await _wizard_advance(message, state, value)


@router.message(ContestWizard.prize)
async def wiz_prize(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


@router.message(ContestWizard.winners)
async def wiz_winners(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) < 1:
        await message.answer("❌ Введите целое число победителей (не меньше 1).")
        return
    await _wizard_advance(message, state, int(text))


@router.message(ContestWizard.starts)
async def wiz_starts(message: types.Message, state: FSMContext):
    dt = _parse_dt(message.text or "")
    if dt is None and (message.text or "").strip().lower() not in ("-", "нет", "пропустить", "skip", "—"):
        await message.answer("❌ Неверный формат даты. Пример: <code>31.12.2026 20:00</code> или отправьте «-».", parse_mode="HTML")
        return
    await _wizard_advance(message, state, dt)


@router.message(ContestWizard.ends)
async def wiz_ends(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    skip = raw.lower() in ("-", "нет", "пропустить", "skip", "—")
    dt = _parse_dt(raw)
    if dt is None and not skip:
        await message.answer("❌ Неверный формат даты. Пример: <code>31.12.2026 23:59</code> или отправьте «-».", parse_mode="HTML")
        return
    data = await state.get_data()
    starts = data.get("starts_at")
    if dt is not None and isinstance(starts, datetime) and dt <= starts:
        await message.answer("❌ Дата окончания должна быть позже даты начала. Введите снова или «-».", parse_mode="HTML")
        return
    await _wizard_advance(message, state, dt)


@router.message(ContestWizard.channels)
async def wiz_channels(message: types.Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    tokens = []
    if raw and raw.lower() not in ("-", "нет", "пропустить", "skip", "—"):
        tokens = [t for t in re.split(r"[,\s]+", raw) if t]
    resolved = []
    async with session_scope() as session:
        for token in tokens:
            ch = await _resolve_or_create_channel(session, bot, token, added_by=message.from_user.id)
            if ch:
                resolved.append(ch.id)
        await state.update_data(channel_ids=resolved)
    await _wizard_advance(message, state, resolved)


@router.message(ContestWizard.conditions)
async def wiz_conditions(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


@router.message(ContestWizard.post)
async def wiz_post(message: types.Message, state: FSMContext):
    await _wizard_advance(message, state, (message.text or "").strip())


_WIZ_FIELDS = [
    ("title", "title", "название", ContestWizard.description, "Шаг 2/10. Введите описание:"),
    ("description", "description", "описание", ContestWizard.image, "Шаг 3/10. Отправьте изображение приза (фото или ссылку, «-» чтобы пропустить):"),
    ("image", "image_url", "изображение", ContestWizard.prize, "Шаг 4/10. Введите название/описание приза:"),
    ("prize", "prize", "приз", ContestWizard.winners, "Шаг 5/10. Введите количество победителей (число):"),
    ("winners", "winners_count", "победители", ContestWizard.starts, "Шаг 6/10. Введите дату начала (ДД.ММ.ГГГГ ЧЧ:ММ) или «-»:"),
    ("starts", "starts_at", "дата начала", ContestWizard.ends, "Шаг 7/10. Введите дату окончания (ДД.ММ.ГГГГ ЧЧ:ММ) или «-»:"),
    ("ends", "ends_at", "дата окончания", ContestWizard.channels, "Шаг 8/10. Введите обязательные каналы через запятую (@username или -100ID), или «-»:"),
    ("channels", "channel_ids", "каналы", ContestWizard.conditions, "Шаг 9/10. Введите дополнительные условия участия, или «-»:"),
    ("conditions", "extra_conditions", "условия", ContestWizard.post, "Шаг 10/10. Введите текст публикации конкурса, или «-»:"),
    ("post", "post_text", "текст публикации", None, None),
]


async def _wizard_advance(message: types.Message, state: FSMContext, value, is_callback: bool = False):
    current = await state.get_state()
    entry = None
    for item in _WIZ_FIELDS:
        if current == f"ContestWizard:{item[0]}":
            entry = item
            break
    if entry is None:
        return

    _, data_key, _, next_state, next_prompt = entry

    # Нормализация пропуска
    if isinstance(value, str) and value.strip().lower() in ("-", "нет", "пропустить", "skip", "—"):
        value = None

    await state.update_data(**{data_key: value})

    if next_state is None:
        # Всё собрано — показываем предпросмотр.
        data = await state.get_data()
        await state.set_state(ContestWizard.confirm)
        await _show_preview(message, state, data, is_callback)
        return

    await state.set_state(next_state)
    await message.answer(next_prompt, reply_markup=_wizard_skip_kb(), parse_mode="HTML")


async def _show_preview(message: types.Message, state: FSMContext, data: dict, is_callback: bool = False):
    starts = data.get("starts_at")
    ends = data.get("ends_at")
    starts_s = starts.strftime("%d.%m.%Y %H:%M") if isinstance(starts, datetime) else "—"
    ends_s = ends.strftime("%d.%m.%Y %H:%M") if isinstance(ends, datetime) else "—"
    channels = data.get("channel_ids") or []
    lines = [
        "📋 <b>Предпросмотр конкурса</b>\n",
        f"📛 Название: <b>{data.get('title') or '—'}</b>",
        f"📝 Описание: {data.get('description') or '—'}",
        f"🖼 Изображение: {'есть' if data.get('image_url') else '—'}",
        f"🎁 Приз: {data.get('prize') or '—'}",
        f"🏆 Победителей: {data.get('winners_count') or 1}",
        f"📅 Начало: {starts_s}",
        f"🏁 Окончание: {ends_s}",
        f"📢 Каналов: {len(channels)}",
        f"📌 Условия: {data.get('extra_conditions') or '—'}",
        f"📰 Текст публикации: {data.get('post_text') or '—'}",
    ]
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Опубликовать", callback_data="adm_wiz_publish")
    kb.button(text="💾 Сохранить как черновик", callback_data="adm_wiz_draft")
    kb.button(text="✏️ Изменить", callback_data="adm_wiz_edit")
    kb.button(text="❌ Отменить", callback_data="adm_wiz_cancel")
    kb.adjust(2, 2)
    text = "\n".join(lines)
    if is_callback:
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "adm_wiz_edit")
async def on_wizard_edit(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except Exception:
        pass
    await state.set_state(ContestWizard.title)
    await callback.message.answer(
        "✏️ Начнём заново. Введите название конкурса:", reply_markup=_wizard_skip_kb(), parse_mode="HTML"
    )


async def _persist_contest(
    message: types.Message,
    state: FSMContext,
    bot: Bot,
    *,
    actor_id: int,
    publish: bool,
    is_callback: bool = False,
):
    data = await state.get_data()
    async with session_scope() as session:
        if not await _is_admin(session, actor_id):
            await message.answer("⛔ Доступ только для администраторов Fable Randomizer.")
            return
        channel_ids = data.get("channel_ids") or []
        status = cs.STATUS_ACTIVE if publish else cs.STATUS_DRAFT
        starts = data.get("starts_at")
        # Если дата старта не задана или в прошлом — конкурс активен сразу.
        if publish and (starts is None or (isinstance(starts, datetime) and starts <= datetime.now(timezone.utc))):
            status = cs.STATUS_ACTIVE
        contest = await cs.create_contest(
            session,
            title=data.get("title") or "Без названия",
            description=data.get("description") or "",
            image_url=data.get("image_url"),
            prize=data.get("prize"),
            winners_count=data.get("winners_count") or 1,
            starts_at=data.get("starts_at"),
            ends_at=data.get("ends_at"),
            extra_conditions=data.get("extra_conditions"),
            post_text=data.get("post_text"),
            status=status,
            created_by=actor_id,
        )
        if channel_ids:
            await cs.set_contest_channels(session, contest.id, channel_ids)
        await log_action(
            session,
            actor_id=actor_id,
            action="contest_publish" if publish else "contest_draft",
            target_type="contest",
            target_id=contest.id,
            details={"title": contest.title, "status": status},
        )
        contest_id = contest.id
        title = contest.title
        prize = contest.prize
        notify_new = publish

    await state.clear()

    if notify_new:
        async with session_scope() as session:
            try:
                await notify.notify_new_contest(session, bot, contest_id=contest_id, title=title, prize=prize)
            except Exception as e:
                logger.error("Ошибка уведомления о новом конкурсе: %s", e)

    kb = InlineKeyboardBuilder()
    kb.button(text="👁 Открыть конкурс", callback_data=f"adm_view_{contest_id}")
    kb.button(text="◀️ В админ-панель", callback_data="adm_panel")
    kb.adjust(1)
    verb = "опубликован" if publish else "сохранён как черновик"
    await message.answer(f"✅ Конкурс «{title}» {verb}! (ID: <code>{contest_id}</code>)", reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "adm_wiz_publish")
async def on_wizard_publish(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    try:
        await callback.answer("Публикую...")
    except Exception:
        pass
    await _persist_contest(callback.message, state, bot, actor_id=callback.from_user.id, publish=True, is_callback=True)


@router.callback_query(F.data == "adm_wiz_draft")
async def on_wizard_draft(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    try:
        await callback.answer("Сохраняю...")
    except Exception:
        pass
    await _persist_contest(callback.message, state, bot, actor_id=callback.from_user.id, publish=False, is_callback=True)


# ===================== УПРАВЛЕНИЕ КОНКУРСАМИ =====================

@router.callback_query(F.data == "adm_mycontests")
async def on_my_contests(callback: types.CallbackQuery):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
        contests = await cs.list_contests(session, category="all", include_archived=True)
        rows = []
        for c in contests:
            rows.append((c, await cs.participants_count(session, c.id)))
    try:
        await callback.answer()
    except Exception:
        pass
    await _render_admin_contest_list(callback.message, rows)


async def _render_admin_contest_list(message, rows):
    if not rows:
        text = "📋 Конкурсов пока нет. Нажмите «➕ Создать конкурс»."
    else:
        lines = ["📋 <b>Все конкурсы (админ):</b>\n"]
        for c, participants in rows:
            status = cs.STATUS_LABELS.get(cs.compute_status(c), c.status)
            ends = c.ends_at.strftime("%d.%m.%Y") if c.ends_at else "—"
            lines.append(
                f"• <b>{c.title}</b> (ID <code>{c.id}</code>) — {status}\n"
                f"   👥 {participants} | 🏆 {c.winners_count} | 🏁 {ends}"
            )
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    for c, _ in rows[:10]:
        kb.button(text=f"⚙️ {c.title[:30]}", callback_data=f"adm_view_{c.id}")
    kb.button(text="➕ Создать конкурс", callback_data="adm_create")
    kb.button(text="◀️ Назад", callback_data="adm_panel")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_view_"))
async def on_view_contest_admin(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("adm_view_", ""))
    await _render_admin_contest(callback, contest_id)


async def _render_admin_contest(callback: types.CallbackQuery, contest_id: int):
    async with session_scope() as session:
        if not await _is_admin(session, callback.from_user.id):
            await _deny(callback)
            return
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            try:
                await callback.answer("Конкурс не найден", show_alert=True)
            except Exception:
                pass
            return
        participants = await cs.participants_count(session, contest_id)
        channels = await cs.get_contest_channels(session, contest_id)
        winners = await ds.get_latest_winners(session, contest_id)
    try:
        await callback.answer()
    except Exception:
        pass

    status = cs.STATUS_LABELS.get(cs.compute_status(contest), contest.status)
    ends = contest.ends_at.strftime("%d.%m.%Y %H:%M") if contest.ends_at else "—"
    lines = [
        f"⚙️ <b>{contest.title}</b> (ID <code>{contest.id}</code>)",
        f"Статус: {status} | 🏁 Окончание: {ends}",
        f"👥 Участников: {participants} | 🏆 Победителей: {contest.winners_count}",
        f"📢 Каналов: {len(channels)} | 📦 Архив: {'да' if contest.is_archived else 'нет'}",
    ]
    if winners:
        lines.append("\n<b>Победители последнего розыгрыша:</b>")
        for w, player in winners:
            name = (player.custom_nickname or player.first_name) if player else f"ID {w.user_id}"
            lines.append(f"   {w.place}. {name}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=f"adm_start_{contest.id}")
    kb.button(text="🏁 Завершить", callback_data=f"adm_finish_{contest.id}")
    kb.button(text="🎲 Провести розыгрыш", callback_data=f"adm_draw_{contest.id}")
    kb.button(text="🔁 Повторный розыгрыш", callback_data=f"adm_redraw_{contest.id}")
    kb.button(text="👥 Участники", callback_data=f"adm_parts_{contest.id}")
    kb.button(text="📊 Статистика", callback_data=f"adm_cstats_{contest.id}")
    kb.button(text="🧾 Проверить честность", callback_data=f"adm_verify_{contest.id}")
    kb.button(text="✏️ Изменить", callback_data=f"adm_edit_{contest.id}")
    kb.button(text="📢 Каналы конкурса", callback_data=f"adm_cchannels_{contest.id}")
    kb.button(text="❌ Отменить конкурс", callback_data=f"adm_cancel_{contest.id}")
    kb.button(text="📦 Архивировать", callback_data=f"adm_archive_{contest.id}")
    kb.button(text="🗑 Удалить", callback_data=f"adm_delete_{contest.id}")
    kb.button(text="◀️ К списку", callback_data="adm_mycontests")
    kb.adjust(2, 2, 2, 2, 2, 2, 1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


async def _admin_action_guard(callback: types.CallbackQuery) -> bool:
    async with session_scope() as session:
        return await _is_admin(session, callback.from_user.id)


@router.callback_query(F.data.startswith("adm_start_"))
async def on_contest_start(callback: types.CallbackQuery, bot: Bot):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_start_", ""))
    async with session_scope() as session:
        contest = await cs.update_contest(session, contest_id, status=cs.STATUS_ACTIVE)
        await log_action(session, actor_id=callback.from_user.id, action="contest_start", target_type="contest", target_id=contest_id)
        title = contest.title
    try:
        await callback.answer("Конкурс запущен")
    except Exception:
        pass
    async with session_scope() as session:
        try:
            await notify.notify_contest_participants(
                session, bot, contest_id=contest_id, event_type="contest_start",
                event_key_suffix="start", text_builder=f"🚀 Конкурс «{title}» начался! Успей принять участие.",
            )
        except Exception:
            pass
    await _render_admin_contest(callback, contest_id)


@router.callback_query(F.data.startswith("adm_finish_"))
async def on_contest_finish(callback: types.CallbackQuery, bot: Bot):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_finish_", ""))
    async with session_scope() as session:
        contest = await cs.update_contest(session, contest_id, status=cs.STATUS_FINISHED)
        await log_action(session, actor_id=callback.from_user.id, action="contest_finish", target_type="contest", target_id=contest_id)
        title = contest.title
    try:
        await callback.answer("Конкурс завершён")
    except Exception:
        pass
    async with session_scope() as session:
        try:
            await notify.notify_contest_participants(
                session, bot, contest_id=contest_id, event_type="contest_finished",
                event_key_suffix="finished", text_builder=f"🏁 Конкурс «{title}» завершён. Скоро объявим победителей!",
            )
        except Exception:
            pass
    await _render_admin_contest(callback, contest_id)


@router.callback_query(F.data.startswith("adm_cancel_"))
async def on_contest_cancel(callback: types.CallbackQuery, bot: Bot):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_cancel_", ""))
    async with session_scope() as session:
        contest = await cs.update_contest(session, contest_id, status=cs.STATUS_CANCELLED)
        await log_action(session, actor_id=callback.from_user.id, action="contest_cancel", target_type="contest", target_id=contest_id)
        title = contest.title
    try:
        await callback.answer("Конкурс отменён")
    except Exception:
        pass
    async with session_scope() as session:
        try:
            await notify.notify_contest_participants(
                session, bot, contest_id=contest_id, event_type="contest_cancelled",
                event_key_suffix="cancelled", text_builder=f"❌ Конкурс «{title}» отменён администратором.",
            )
        except Exception:
            pass
    await _render_admin_contest(callback, contest_id)


@router.callback_query(F.data.startswith("adm_archive_"))
async def on_contest_archive(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_archive_", ""))
    async with session_scope() as session:
        await cs.archive_contest(session, contest_id)
        await log_action(session, actor_id=callback.from_user.id, action="contest_archive", target_type="contest", target_id=contest_id)
    try:
        await callback.answer("Архивировано")
    except Exception:
        pass
    await _render_admin_contest(callback, contest_id)


@router.callback_query(F.data.startswith("adm_delete_"))
async def on_contest_delete_confirm(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_delete_", ""))
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"adm_delete_yes_{contest_id}")
    kb.button(text="❌ Отмена", callback_data=f"adm_view_{contest_id}")
    kb.adjust(2)
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text(
            "⚠️ Удалить конкурс вместе с участниками и результатами? Это необратимо.",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_delete_yes_"))
async def on_contest_delete(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_delete_yes_", ""))
    async with session_scope() as session:
        try:
            await cs.delete_contest(session, contest_id)
            await log_action(session, actor_id=callback.from_user.id, action="contest_delete", target_type="contest", target_id=contest_id)
        except cs.ContestError:
            pass
    try:
        await callback.answer("Удалено")
    except Exception:
        pass
    await on_my_contests(callback)


@router.callback_query(F.data.startswith("adm_parts_"))
async def on_contest_participants(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_parts_", ""))
    async with session_scope() as session:
        entries = await cs.list_entries(session, contest_id, active_only=False)
        rows = []
        for e in entries:
            p = await session.get(Player, e.user_id)
            rows.append((e, p))
    try:
        await callback.answer()
    except Exception:
        pass
    if not rows:
        text = "👥 Участников пока нет."
    else:
        lines = [f"👥 <b>Участники конкурса {contest_id} ({len(rows)}):</b>\n"]
        for e, p in rows[:50]:
            name = (p.custom_nickname or p.first_name) if p else f"ID {e.user_id}"
            uname = f" (@{p.username})" if p and p.username else ""
            mark = "🟢" if e.status == "active" else "🚪"
            lines.append(f"{mark} {name}{uname} <code>{e.user_id}</code>")
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_cstats_"))
async def on_contest_stats(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_cstats_", ""))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            return
        active = await cs.participants_count(session, contest_id)
        all_entries = await cs.list_entries(session, contest_id, active_only=False)
        winners = await cs.winners_count(session, contest_id)
        logs = await ds.get_draw_history(session, contest_id)
    try:
        await callback.answer()
    except Exception:
        pass
    lines = [
        f"📊 <b>Статистика конкурса «{contest.title}»</b>\n",
        f"👥 Активных участников: <b>{active}</b>",
        f"👤 Всего записей: <b>{len(all_entries)}</b>",
        f"🚪 Отменили участие: <b>{sum(1 for e in all_entries if e.status == 'left')}</b>",
        f"🏆 Победителей: <b>{winners}</b>",
        f"🎲 Розыгрышей: <b>{len(logs)}</b>",
    ]
    if logs:
        lines.append("\n<b>История розыгрышей:</b>")
        for lg in logs:
            reason = f" — {lg.reason}" if lg.reason else ""
            lines.append(f"• Раунд {lg.round}: {lg.winners_count} победителей (алгоритм {lg.algorithm_version}){reason}")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_draw_"))
async def on_contest_draw(callback: types.CallbackQuery, bot: Bot):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_draw_", ""))
    await _do_draw(callback, bot, contest_id, reason=None)


@router.callback_query(F.data.startswith("adm_redraw_"))
async def on_contest_redraw(callback: types.CallbackQuery, bot: Bot):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_redraw_", ""))
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Заново среди всех", callback_data=f"adm_redrawall_{contest_id}")
    kb.button(text="🆕 Исключить прошлых победителей", callback_data=f"adm_redrawnew_{contest_id}")
    kb.button(text="◀️ Назад", callback_data=f"adm_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text(
            "🔁 <b>Повторный розыгрыш</b>\n\nПрошлые результаты сохранятся, будет создан новый раунд.\n"
            "Выберите режим:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_redrawall_"))
async def on_contest_redraw_all(callback: types.CallbackQuery, bot: Bot):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_redrawall_", ""))
    await _do_draw(callback, bot, contest_id, reason="Повторный розыгрыш среди всех участников")


@router.callback_query(F.data.startswith("adm_redrawnew_"))
async def on_contest_redraw_new(callback: types.CallbackQuery, bot: Bot):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_redrawnew_", ""))
    await _do_draw(
        callback, bot, contest_id,
        reason="Повторный розыгрыш без прошлых победителей",
        exclude_previous_winners=True,
    )


@router.callback_query(F.data.startswith("adm_verify_"))
async def on_contest_verify(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_verify_", ""))
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
                .button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
                .as_markup(),
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
            f"   победители: {', '.join(str(x) for x in stored) or '—'}"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


async def _do_draw(
    callback: types.CallbackQuery,
    bot: Bot,
    contest_id: int,
    reason: str | None,
    *,
    exclude_previous_winners: bool = False,
):
    async with session_scope() as session:
        try:
            result = await ds.run_draw(
                session,
                contest_id=contest_id,
                executed_by=callback.from_user.id,
                reason=reason,
                exclude_previous_winners=exclude_previous_winners,
            )
        except ds.DrawError as e:
            try:
                await callback.answer(str(e), show_alert=True)
            except Exception:
                pass
            return
        await log_action(
            session, actor_id=callback.from_user.id,
            action="contest_redraw" if reason else "contest_draw",
            target_type="contest", target_id=contest_id,
            details={"round": result.round, "winners": result.winners, "seed": result.seed},
        )
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
        f"Раунд {result.round}: выбрано {len(result.winners)} победителей из {len(result.participants)} участников."
    )
    await _render_admin_contest(callback, contest_id)


# ===================== РЕДАКТИРОВАНИЕ КОНКУРСА =====================

class EditContestWizard(StatesGroup):
    field = State()
    value = State()


EDITABLE_FIELDS = {
    "title": "название",
    "description": "описание",
    "prize": "приз",
    "winners_count": "число победителей",
    "extra_conditions": "условия",
    "post_text": "текст публикации",
}


@router.callback_query(F.data.startswith("adm_edit_"))
async def on_edit_contest(callback: types.CallbackQuery, state: FSMContext):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_edit_", ""))
    await state.update_data(edit_contest_id=contest_id)
    kb = InlineKeyboardBuilder()
    for field, label in EDITABLE_FIELDS.items():
        kb.button(text=f"✏️ {label}", callback_data=f"adm_editf_{contest_id}_{field}")
    kb.button(text="🏆 Изменить число победителей", callback_data=f"adm_editf_{contest_id}_winners_count")
    kb.button(text="📢 Добавить канал", callback_data=f"adm_addch_{contest_id}")
    kb.button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
    kb.adjust(2)
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.edit_text("✏️ Что изменить?", reply_markup=kb.as_markup())
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm_editf_"))
async def on_edit_field(callback: types.CallbackQuery, state: FSMContext):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    _, _, contest_id, field = callback.data.split("_", 3)
    await state.update_data(edit_contest_id=int(contest_id), edit_field=field)
    await state.set_state(EditContestWizard.value)
    try:
        await callback.answer()
    except Exception:
        pass
    label = EDITABLE_FIELDS.get(field, field)
    await callback.message.answer(f"✏️ Введите новое значение для «{label}»:")


@router.message(EditContestWizard.value)
async def on_edit_value(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    contest_id = data.get("edit_contest_id")
    field = data.get("edit_field")
    value = (message.text or "").strip()
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
        if field == "channel":
            channel = await _resolve_or_create_channel(session, bot, value, added_by=message.from_user.id)
            if not channel:
                await message.answer("❌ Не удалось найти канал. Убедитесь, что бот добавлен в канал.")
                return
            await cs.add_contest_channel(session, contest_id, channel.id)
            await log_action(session, actor_id=message.from_user.id, action="contest_channel_add",
                             target_type="contest", target_id=contest_id, details={"channel_id": channel.id})
            await state.clear()
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
            await message.answer("✅ Канал добавлен в конкурс.", reply_markup=kb.as_markup())
            return
        if field == "winners_count":
            if not value.isdigit() or int(value) < 1:
                await message.answer("❌ Введите число не меньше 1.")
                return
            value = int(value)
        await cs.update_contest(session, contest_id, **{field: value})
        await log_action(
            session, actor_id=message.from_user.id, action="contest_edit",
            target_type="contest", target_id=contest_id, details={"field": field},
        )
        # Уведомляем участников об изменении условий.
        try:
            await notify.notify_contest_participants(
                session, bot, contest_id=contest_id, event_type="conditions_changed",
                event_key_suffix=f"edit_{field}", text_builder="📝 Условия конкурса были изменены администратором. Проверьте детали!",
            )
        except Exception:
            pass
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
    await message.answer("✅ Изменения сохранены.", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("adm_addch_"))
async def on_add_channel_to_contest(callback: types.CallbackQuery, state: FSMContext):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_addch_", ""))
    await state.update_data(edit_contest_id=contest_id, edit_field="channel")
    await state.set_state(EditContestWizard.value)
    try:
        await callback.answer()
    except Exception:
        pass
    await callback.message.answer("📢 Введите @username или -100ID канала для добавления:")


# ===================== УПРАВЛЕНИЕ КАНАЛАМИ =====================

async def _resolve_or_create_channel(session, bot: Bot, token: str, *, added_by: int) -> Channel | None:
    token = token.strip()
    chat = None
    try:
        if token.lstrip("-").isdigit():
            chat = await bot.get_chat(int(token))
        else:
            chat = await bot.get_chat(token if token.startswith("@") else f"@{token}")
    except Exception as e:
        logger.warning("Не удалось получить канал %s: %s", token, e)
        return None

    existing = await session.scalar(select(Channel).where(Channel.chat_id == chat.id))
    if existing:
        existing.title = chat.title or existing.title
        existing.username = chat.username or existing.username
        existing.is_active = True
        await session.flush()
        return existing

    channel = Channel(
        chat_id=chat.id,
        username=chat.username,
        title=chat.title,
        invite_link=None,
        is_active=True,
        added_by=added_by,
    )
    session.add(channel)
    await session.flush()
    return channel


async def _check_bot_rights(bot: Bot, chat_id: int) -> str | None:
    """Возвращает предупреждение, если у бота нет прав в канале, иначе None."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        status = getattr(member, "status", None)
        if status not in ("administrator", "creator"):
            return "⚠️ Бот не является администратором этого канала — проверка подписок может не работать."
        if status == "administrator" and not getattr(member, "can_restrict_members", True):
            return "⚠️ У бота нет прав для полной проверки подписчиков канала."
        return None
    except Exception as e:
        return f"⚠️ Не удалось проверить права бота в канале: {e}"


@router.message(Command("addchannel"))
async def cmd_addchannel(message: types.Message, command: CommandObject, bot: Bot):
    if not message.from_user:
        return
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
    args = (command.args or "").strip()
    if not args:
        await message.answer("Использование: <code>/addchannel @username</code> или <code>/addchannel -1001234567890</code>", parse_mode="HTML")
        return
    async with session_scope() as session:
        channel = await _resolve_or_create_channel(session, bot, args, added_by=message.from_user.id)
        if not channel:
            await message.answer("❌ Не удалось найти канал. Убедитесь, что бот добавлен в канал как администратор.")
            return
        await log_action(session, actor_id=message.from_user.id, action="channel_add", target_type="channel", target_id=channel.id)
        title = channel.title or channel.username or channel.chat_id
        warn = await _check_bot_rights(bot, channel.chat_id) if channel.chat_id else None
    text = f"✅ Канал «{title}» добавлен."
    if warn:
        text += f"\n\n{warn}"
    await message.answer(text)


@router.message(Command("channels"))
async def cmd_channels(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
        channels = (await session.scalars(select(Channel).order_by(Channel.id.asc()))).all()
    if not channels:
        await message.answer("📢 Каналы не добавлены. Используйте <code>/addchannel @username</code>.", parse_mode="HTML")
        return
    lines = ["📢 <b>Каналы:</b>\n"]
    for ch in channels:
        title = ch.title or ch.username or ch.chat_id
        uname = f" @{ch.username}" if ch.username else ""
        status = "🟢" if ch.is_active else "🔴"
        lines.append(f"{status} <b>{title}</b>{uname} — ID <code>{ch.chat_id}</code>")
    lines.append("\n<i>Удалить: /removechannel @username или ID</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "adm_channels")
async def on_channels_button(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    try:
        await callback.answer()
    except Exception:
        pass
    await cmd_channels(callback.message)


@router.message(Command("removechannel"))
async def cmd_removechannel(message: types.Message, command: CommandObject, bot: Bot):
    if not message.from_user:
        return
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
    args = (command.args or "").strip()
    if not args:
        await message.answer("Использование: <code>/removechannel @username</code> или <code>/removechannel -100ID</code>", parse_mode="HTML")
        return
    async with session_scope() as session:
        channel = None
        if args.lstrip("-").isdigit():
            channel = await session.scalar(select(Channel).where(Channel.chat_id == int(args)))
        else:
            uname = args.lstrip("@")
            channel = await session.scalar(select(Channel).where(Channel.username == uname))
        if not channel:
            await message.answer("❌ Канал не найден.")
            return
        # Удаляем связи с конкурсами и сам канал.
        links = (await session.scalars(select(ContestChannel).where(ContestChannel.channel_id == channel.id))).all()
        for link in links:
            await session.delete(link)
        await session.delete(channel)
        await session.flush()
        await log_action(session, actor_id=message.from_user.id, action="channel_remove", target_type="channel", target_id=channel.id)
    await message.answer("✅ Канал удалён.")


@router.callback_query(F.data.startswith("adm_cchannels_"))
async def on_contest_channels_admin(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    contest_id = int(callback.data.replace("adm_cchannels_", ""))
    async with session_scope() as session:
        channels = await cs.get_contest_channels(session, contest_id)
    try:
        await callback.answer()
    except Exception:
        pass
    if not channels:
        text = "📢 У конкурса нет обязательных каналов."
    else:
        text = "📢 <b>Каналы конкурса:</b>\n" + "\n".join(
            f"• {ch.title or ch.username or ch.chat_id}" for ch in channels
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Добавить канал", callback_data=f"adm_addch_{contest_id}")
    kb.button(text="◀️ К конкурсу", callback_data=f"adm_view_{contest_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


# ===================== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ =====================

class UserSearchWizard(StatesGroup):
    user_id = State()


@router.callback_query(F.data == "adm_users")
async def on_admin_users(callback: types.CallbackQuery, state: FSMContext):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    try:
        await callback.answer()
    except Exception:
        pass
    await state.set_state(UserSearchWizard.user_id)
    await callback.message.answer("👤 Введите Telegram ID пользователя для поиска:")


@router.message(UserSearchWizard.user_id)
async def on_user_search(message: types.Message, state: FSMContext):
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
    value = (message.text or "").strip()
    await state.clear()
    if not value.lstrip("-").isdigit():
        await message.answer("❌ Telegram ID должен состоять из цифр.")
        return
    await _show_user_card(message, int(value))


@router.message(Command("finduser"))
async def cmd_finduser(message: types.Message, command: CommandObject):
    if not message.from_user:
        return
    async with session_scope() as session:
        if not await _is_admin(session, message.from_user.id):
            await _deny(message)
            return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer("Использование: <code>/finduser ID</code>", parse_mode="HTML")
        return
    await _show_user_card(message, int(args))


async def _show_user_card(message, target_id: int):
    async with session_scope() as session:
        player = await session.get(Player, target_id)
        if not player:
            await message.answer(f"❌ Пользователь <code>{target_id}</code> не найден.", parse_mode="HTML")
            return
        invited = await session.scalar(select(func.count(Referral.id)).where(Referral.inviter_id == target_id)) or 0
        stats = await cs.get_user_stats(session, target_id)
        entries = await cs.get_user_entries(session, target_id)
        wins = await session.scalar(select(func.count(Winner.id)).where(Winner.user_id == target_id)) or 0
    name = player.custom_nickname or player.first_name
    uname = f"@{player.username}" if player.username else "—"
    status = "🔴 Заблокирован" if player.is_globally_banned else "🟢 Активен"
    reg = player.registered_at.strftime("%d.%m.%Y") if player.registered_at else "—"
    lines = [
        f"👤 <b>{name}</b> <code>{player.telegram_id}</code>",
        f"📛 {uname} | Статус: {status}",
        f"🗓 Регистрация: {reg}",
        f"👥 Пригласил: {invited}",
        f"🎯 Активных: {stats['active']} | 🏁 Завершённых: {stats['finished']} | 🏆 Побед: {wins}",
    ]
    if entries:
        lines.append("\n<b>Последние конкурсы:</b>")
        for e, c in entries[:5]:
            lines.append(f"• {c.title} — {cs.STATUS_LABELS.get(cs.compute_status(c), c.status)}")
    kb = InlineKeyboardBuilder()
    if player.is_globally_banned:
        kb.button(text="✅ Разблокировать", callback_data=f"adm_unban_{target_id}")
    else:
        kb.button(text="🚫 Заблокировать", callback_data=f"adm_ban_{target_id}")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_ban_"))
async def on_ban_user(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    target_id = int(callback.data.replace("adm_ban_", ""))
    async with session_scope() as session:
        player = await session.get(Player, target_id)
        if player:
            player.is_globally_banned = True
            player.global_ban_reason = "Блокировка администратором Randomizer"
            await session.flush()
            await log_action(session, actor_id=callback.from_user.id, action="user_ban", target_type="player", target_id=target_id)
    try:
        await callback.answer("Пользователь заблокирован")
    except Exception:
        pass
    await callback.message.answer(f"🚫 Пользователь <code>{target_id}</code> заблокирован. История сохранена.", parse_mode="HTML")


@router.callback_query(F.data.startswith("adm_unban_"))
async def on_unban_user(callback: types.CallbackQuery):
    if not await _admin_action_guard(callback):
        await _deny(callback)
        return
    target_id = int(callback.data.replace("adm_unban_", ""))
    async with session_scope() as session:
        player = await session.get(Player, target_id)
        if player:
            player.is_globally_banned = False
            player.global_ban_reason = None
            await session.flush()
            await log_action(session, actor_id=callback.from_user.id, action="user_unban", target_type="player", target_id=target_id)
    try:
        await callback.answer("Пользователь разблокирован")
    except Exception:
        pass
    await callback.message.answer(f"✅ Пользователь <code>{target_id}</code> разблокирован.", parse_mode="HTML")
