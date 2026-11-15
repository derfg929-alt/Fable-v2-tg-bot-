"""Пользовательские обработчики Fable Randomizer.

Главное меню, конкурсы, участие, профиль, приглашения, победители, история и
помощь. Все кнопки меню имеют настоящие обработчики; данные берутся из БД.
"""
from __future__ import annotations

import logging

from aiogram import Router, types, F, Bot
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services import contest_service as cs
from app.services import contest_settings as cset
from app.services import referral_service as ref
from app.services import draw_service as ds
from app.services import notifications_service as notify
from app.services import randomizer_settings as rset
from app.services import ticket_service as tickets
from app.services.randomizer_settings import get_bot_username

logger = logging.getLogger("fable.randomizer.handlers")
router = Router(name="randomizer_router")

MENU_CONTESTS = "🎁 Конкурсы"
MENU_MY_CONTESTS = "📣 Мои конкурсы"
MENU_CREATE = "➕ Создать конкурс"
MENU_PROFILE = "👤 Мой профиль"
MENU_INVITE = "👥 Пригласить друзей"
MENU_WINNERS = "🏆 Победители"
MENU_HISTORY = "📜 История участия"
MENU_HELP = "ℹ️ Помощь"

WELCOME_BACK = "С возвращением в Fable Randomizer! ✨"
WELCOME_NEW = "Добро пожаловать в Fable Randomizer! ✨"

GREETING_BODY = (
    "🎲 <b>Fable Randomizer</b> — конкурсы и розыгрыши призов.\n\n"
    "🎁 Участвуй в конкурсах и выигрывай призы\n"
    "📣 Создавай свои конкурсы — для своего канала или без канала\n"
    "👥 Приглашай друзей и повышай свои шансы\n\n"
    "Выбери раздел в меню ниже 👇"
)


def _menu_keyboard(mini_app_url: str | None = None):
    kb = ReplyKeyboardBuilder()
    kb.button(text=MENU_CONTESTS)
    kb.button(text=MENU_MY_CONTESTS)
    kb.button(text=MENU_CREATE)
    kb.button(text=MENU_PROFILE)
    kb.button(text=MENU_INVITE)
    kb.button(text=MENU_WINNERS)
    kb.button(text=MENU_HISTORY)
    kb.button(text=MENU_HELP)
    kb.adjust(2, 2, 2, 2)
    if mini_app_url:
        kb.button(text="🚀 Открыть Mini App", web_app=types.WebAppInfo(url=mini_app_url))
    return kb.as_markup(resize_keyboard=True)


async def _resolve_bot_username(bot: Bot, session) -> str | None:
    """Определяет username бота: из настроек, затем через Telegram API."""
    username = await get_bot_username(session)
    if username:
        return username
    try:
        me = await bot.get_me()
        candidate = getattr(me, "username", None)
        if isinstance(candidate, str) and candidate.strip():
            candidate = candidate.strip().lstrip("@")
            await rset.set_bot_username(session, candidate)
            return candidate
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось получить username бота из Telegram API: %s", e)
    return None


async def _mini_app_url(session) -> str | None:
    from app.config import get_settings

    return get_settings().effective_mini_app_url or None


async def _is_admin(session, user_id: int) -> bool:
    return await rset.is_admin(session, user_id)


# ===================== /start И РЕГИСТРАЦИЯ =====================

@router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: types.Message, command: CommandObject, bot: Bot):
    await _handle_start(message, bot, command.args)


@router.message(CommandStart())
async def cmd_start(message: types.Message, bot: Bot):
    await _handle_start(message, bot, None)


@router.message(F.text.casefold().in_(["старт", "!старт"]))
async def cmd_start_text(message: types.Message, bot: Bot):
    await _handle_start(message, bot, None)


async def _handle_start(message: types.Message, bot: Bot, payload: str | None):
    if not message.from_user:
        return
    inviter_id = ref.parse_referral_payload(payload)
    contest_deeplink = _parse_contest_payload(payload)
    invited_ok = False
    async with session_scope() as session:
        reg = await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name or "Игрок",
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        created = reg.created

        if created and inviter_id and inviter_id != message.from_user.id:
            invited_ok = await ref.register_referral(
                session, inviter_id=inviter_id, invited_id=message.from_user.id
            )

        mini_url = await _mini_app_url(session)
        # Автоматически формируем реферальную ссылку, чтобы у каждого была своя.
        bot_username = await _resolve_bot_username(bot, session)
        invited_count = await ref.count_invited(session, message.from_user.id)
        active_contests = await cs.list_contests(session, category="active")
        contest_total = len(active_contests)

    ref_link = ref.build_referral_link(bot_username, message.from_user.id) if bot_username else None

    head = WELCOME_NEW if created else WELCOME_BACK
    lines = [head, "", GREETING_BODY]
    if created and invited_ok:
        lines.append("\n🎉 Ты пришёл по приглашению друга — удачи в конкурсах!")
    elif created and inviter_id and not invited_ok:
        lines.append("\n<i>Реферальная ссылка недействительна, но ты всё равно в игре!</i>")
    lines.append(f"\n🔥 Активных конкурсов сейчас: <b>{contest_total}</b>")
    lines.append(f"👥 Приглашено друзей: <b>{invited_count}</b>")
    if ref_link:
        lines.append(f"\n🔗 Твоя ссылка для друзей:\n<code>{ref_link}</code>")

    await message.answer("\n".join(lines), reply_markup=_menu_keyboard(mini_url), parse_mode="HTML")
    await _send_quick_menu(message)

    if contest_deeplink:
        await _send_contest_message(message, contest_deeplink)


def _parse_contest_payload(payload: str | None) -> int | None:
    """Извлекает ID конкурса из диплинка /start c_<id>."""
    if not payload:
        return None
    payload = payload.strip()
    if payload.startswith("c_") and payload[2:].isdigit():
        return int(payload[2:])
    return None


async def _send_contest_message(message: types.Message, contest_id: int):
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            return
        channels = await cs.get_contest_channels(session, contest_id)
        entry = await cs.get_entry(session, contest_id, message.from_user.id)
        is_participant = bool(entry and entry.status == "active")
        text = await _contest_page_text(session, contest)
        markup = _contest_page_keyboard(contest, channels, is_participant=is_participant)
    try:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass


async def _send_quick_menu(message: types.Message):
    """Отправляет компактное inline-меню быстрых действий сразу после приветствия."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Активные конкурсы", callback_data="rz_list_active")
    kb.button(text="📣 Мои конкурсы", callback_data="uc_my_contests")
    kb.button(text="➕ Создать конкурс", callback_data="uc_create")
    kb.button(text="🏆 Победители", callback_data="rz_show_winners")
    kb.button(text="👥 Пригласить друзей", callback_data="rz_invite")
    kb.adjust(2, 2, 1)
    try:
        await message.answer("⚡️ <b>Быстрые действия:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        pass


# ===================== КОНКУРСЫ =====================

@router.message(Command("contests"))
@router.message(F.text.casefold().in_(["конкурсы", "!конкурсы", MENU_CONTESTS.casefold()]))
async def cmd_contests(message: types.Message):
    await _send_contests_menu(message, category="active")


def _category_keyboard(active: str = "active"):
    kb = InlineKeyboardBuilder()
    kb.button(text=("✅ " if active == "active" else "") + "Активные", callback_data="rz_list_active")
    kb.button(text=("✅ " if active == "upcoming" else "") + "Скоро начнутся", callback_data="rz_list_upcoming")
    kb.button(text=("✅ " if active == "finished" else "") + "Завершённые", callback_data="rz_list_finished")
    kb.adjust(3)
    return kb


async def _send_contests_menu(message: types.Message, *, category: str, edit: bool = False):
    async with session_scope() as session:
        contests = await cs.list_contests(session, category=category)
        counts = {}
        for c in contests:
            counts[c.id] = await cs.participants_count(session, c.id)

    kb = _category_keyboard(category)
    if not contests:
        text = "Пока нет активных конкурсов. Загляни позже! 🎁"
        if category == "upcoming":
            text = "Пока нет конкурсов, которые скоро начнутся. Загляни позже! 🎁"
        elif category == "finished":
            text = "Пока нет завершённых конкурсов. Загляни позже! 🎁"
    else:
        titles = {"active": "🟢 Активные конкурсы", "upcoming": "⏳ Скоро начнутся", "finished": "🏁 Завершённые конкурсы"}
        text = f"🎁 <b>{titles.get(category, 'Конкурсы')}</b>\n\n"
        for c in contests:
            status = cs.STATUS_LABELS.get(cs.compute_status(c), c.status)
            text += (
                f"• <b>{c.title}</b> — {status}\n"
                f"   👥 Участников: {counts.get(c.id, 0)} | 🏆 Победителей: {c.winners_count}\n"
            )
            kb.button(text=f"👁 {c.title[:32]}", callback_data=f"rz_view_{c.id}")
        kb.adjust(3, 1)

    markup = kb.as_markup()
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("rz_list_"))
async def on_list_category(callback: types.CallbackQuery):
    category = callback.data.replace("rz_list_", "")
    try:
        await callback.answer()
    except Exception:
        pass
    await _send_contests_menu(callback.message, category=category, edit=True)


async def _contest_page_text(session, contest) -> str:
    channels = await cs.get_contest_channels(session, contest.id)
    participants = await cs.participants_count(session, contest.id)
    winners_total = await cs.winners_count(session, contest.id)
    settings = cset.read_settings(contest)
    status = cs.compute_status(contest)
    status_label = cs.STATUS_LABELS.get(status, contest.status)
    starts = contest.starts_at.strftime("%d.%m.%Y %H:%M") if contest.starts_at else "—"
    ends = contest.ends_at.strftime("%d.%m.%Y %H:%M") if contest.ends_at else "—"
    lines = [
        f"🎁 <b>{contest.title}</b>",
        f"Статус: {status_label}",
        "",
        f"📝 {contest.description or '—'}",
    ]
    if contest.prize:
        lines.append(f"🎁 Приз: <b>{contest.prize}</b>")
    lines += [
        "",
        f"📅 Начало: {starts}",
        f"🏁 Окончание: {ends}",
        f"🏆 Победителей: {contest.winners_count}",
        f"👥 Участников: {participants}",
        f"🥇 Выбрано победителей: {winners_total}",
    ]
    mechanics = cset.public_summary(settings)
    if mechanics:
        lines.append("\n<b>Как повысить шансы:</b>")
        lines += [f"• {m}" for m in mechanics]
    if channels:
        lines.append("\n📢 <b>Обязательные каналы:</b>")
        for ch in channels:
            title = ch.title or ch.username or str(ch.chat_id)
            lines.append(f"• {title}")
    if contest.extra_conditions:
        lines.append(f"\n📌 <b>Условия участия:</b>\n{contest.extra_conditions}")
    if status in (cs.STATUS_FINISHED, cs.STATUS_CANCELLED):
        winners = await ds.get_latest_winners(session, contest.id)
        reserves = await ds.get_latest_reserves(session, contest.id)
        if winners:
            lines.append("\n🏆 <b>Победители:</b>")
            for w, player in winners:
                name = (player.custom_nickname or player.first_name) if player else f"ID {w.user_id}"
                uname = f" (@{player.username})" if player and player.username else ""
                prize = f" — 🎁 {w.prize}" if w.prize else ""
                lines.append(f"   {w.place}. {name}{uname}{prize}")
            if reserves:
                lines.append("\n🔄 <b>Запасные:</b>")
                for w, player in reserves:
                    name = (player.custom_nickname or player.first_name) if player else f"ID {w.user_id}"
                    lines.append(f"   {w.place}. {name}")
        elif status == cs.STATUS_FINISHED:
            lines.append("\n🏆 Победители ещё не определены — следи за обновлениями!")
    return "\n".join(lines)


def _contest_page_keyboard(contest, channels, *, is_participant: bool, settings: dict | None = None):
    settings = cset.normalize_settings(settings if settings is not None else getattr(contest, "settings_json", None))
    kb = InlineKeyboardBuilder()
    status = cs.compute_status(contest)
    for ch in channels:
        url = None
        if ch.username:
            url = f"https://t.me/{ch.username.lstrip('@')}"
        elif ch.invite_link:
            url = ch.invite_link
        if url:
            kb.button(text=f"📢 {ch.title or ch.username}", url=url)
    if status in (cs.STATUS_ACTIVE, cs.STATUS_SCHEDULED):
        if is_participant:
            if contest.allow_leave:
                kb.button(text="🚪 Отменить участие", callback_data=f"rz_leave_{contest.id}")
            if settings.get("code_words"):
                kb.button(text="🔑 Ввести кодовое слово", callback_data=f"rz_codeword_{contest.id}")
            if settings.get("sponsor_tasks"):
                kb.button(text="🎯 Задание спонсора", callback_data=f"rz_task_{contest.id}")
            if settings.get("share_button"):
                kb.button(text="📤 Поделиться", callback_data=f"rz_share_{contest.id}")
        else:
            kb.button(text="🎉 Участвовать", callback_data=f"rz_join_{contest.id}")
    if settings.get("show_participants"):
        kb.button(text="👥 Участники", callback_data=f"rz_parts_{contest.id}")
    if settings.get("leaderboard"):
        kb.button(text="🏅 Рейтинг", callback_data=f"rz_top_{contest.id}")
    kb.button(text="◀️ К конкурсам", callback_data="rz_list_active")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("rz_view_"))
async def on_view_contest(callback: types.CallbackQuery, bot: Bot):
    contest_id = int(callback.data.replace("rz_view_", ""))
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            await callback.message.answer("❌ Конкурс не найден.")
            return
        channels = await cs.get_contest_channels(session, contest_id)
        entry = await cs.get_entry(session, contest_id, callback.from_user.id)
        is_participant = bool(entry and entry.status == "active")
        text = await _contest_page_text(session, contest)
        markup = _contest_page_keyboard(contest, channels, is_participant=is_participant)
        image = contest.image_url

    # Если у конкурса есть изображение приза — показываем его с описанием.
    if image:
        try:
            await callback.message.answer_photo(photo=image, caption=text, reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("rz_join_"))
async def on_join_contest(callback: types.CallbackQuery, bot: Bot):
    contest_id = int(callback.data.replace("rz_join_", ""))
    await _attempt_join(callback, bot, contest_id)


@router.message(Command("join"))
async def cmd_join(message: types.Message, command: CommandObject, bot: Bot):
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer("Использование: <code>/join ID</code> — где ID это номер конкурса.", parse_mode="HTML")
        return
    contest_id = int(args)
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
    if not contest:
        await message.answer("❌ Конкурс с таким ID не найден.")
        return
    await _attempt_join(message, bot, contest_id, is_message=True)


async def _attempt_join(event, bot: Bot, contest_id: int, is_message: bool = False):
    user = event.from_user
    missing_channels = None
    join_text = "Ты успешно зарегистрирован в конкурсе! 🎉\n\nЖелаем удачи! Следи за результатами Fable Randomizer"
    join_kb = None
    async with session_scope() as session:
        try:
            bonus = await ref.get_referral_bonus_units(session, user.id)
            await cs.join_contest(
                session,
                bot,
                contest_id=contest_id,
                user_id=user.id,
                first_name=user.first_name or "Игрок",
                username=user.username,
                bonus_units=bonus,
            )
        except cs.NotSubscribedError as e:
            missing_channels = e.missing
        except cs.AlreadyParticipatingError:
            await _reply(event, "Ты уже участвуешь в этом конкурсе!", is_message)
            return
        except cs.OwnerCannotJoinError:
            await _reply(
                event,
                "🙅 Ты организатор этого конкурса и не можешь в нём участвовать — но можешь следить за статистикой!",
                is_message,
            )
            return
        except cs.BannedFromContestError:
            await _reply(event, "🚫 Вы заблокированы администратором и не можете участвовать.", is_message)
            return
        except cs.ContestClosedError:
            await _reply(event, "❌ Конкурс недоступен для участия.", is_message)
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка участия в конкурсе: %s", e)
            await _reply(event, "❌ Не удалось зарегистрироваться. Попробуйте позже.", is_message)
            return

        contest = await cs.get_contest(session, contest_id)
        settings = cset.read_settings(contest) if contest else cset.default_settings()
        entry = await tickets.get_entry(session, contest_id, user.id)
        ticket_count = int(entry.tickets) if entry else 1
        lines = [
            f"Ты успешно зарегистрирован в конкурсе! 🎉",
            "",
            f"🎟 Твоих билетов: <b>{ticket_count}</b>. Чем больше билетов — тем выше шанс победы!",
        ]
        if settings.get("code_words"):
            lines.append("🔑 Введи кодовое слово (кнопка ниже), чтобы получить ещё билеты.")
        if settings.get("sponsor_tasks") and settings.get("sponsor_task_text"):
            lines.append(f"🎯 Задание спонсора: {settings['sponsor_task_text']}")
        if settings.get("tickets_per_ref", 1) > 1:
            lines.append(f"👥 За каждого друга, выполнившего условия: +{settings['tickets_per_ref']} билет(ов).")
        join_text = "\n".join(lines)

        # Кнопки дополнительных действий сразу после участия.
        kb = InlineKeyboardBuilder()
        if settings.get("code_words"):
            kb.button(text="🔑 Ввести кодовое слово", callback_data=f"rz_codeword_{contest_id}")
        if settings.get("sponsor_tasks"):
            kb.button(text="🎯 Я выполнил задание", callback_data=f"rz_task_{contest_id}")
        if settings.get("share_button"):
            kb.button(text="📤 Поделиться", callback_data=f"rz_share_{contest_id}")
        kb.button(text="👤 Мои билеты", callback_data="rz_my_tickets")
        kb.adjust(1)
        join_kb = kb.as_markup()

        # Уведомление об успешной регистрации.
        try:
            await notify.notify_user(
                session,
                bot,
                user_id=user.id,
                event_type="registration_success",
                event_key=f"registration_success:{contest_id}",
                text="Ты успешно зарегистрирован в конкурсе! 🎉",
                contest_id=contest_id,
            )
        except Exception:
            pass

    if missing_channels is not None:
        await _show_subscription_prompt(event, bot, contest_id, missing_channels, is_message)
        # В группах участник может не увидеть сообщение — дублируем напоминание в ЛС.
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        chat_type = chat.type if chat else ChatType.PRIVATE
        if chat_type != ChatType.PRIVATE:
            kb = _subscription_keyboard(contest_id, missing_channels)
            async with session_scope() as notif_session:
                await notify.notify_user(
                    notif_session,
                    bot,
                    user_id=user.id,
                    event_type="subscribe_required",
                    event_key=f"subscribe_required:{contest_id}",
                    text="Для участия необходимо подписаться на все указанные каналы",
                    contest_id=contest_id,
                    reply_markup=kb,
                )
        return

    await _reply(event, join_text, is_message, reply_markup=join_kb)


async def _reply(event, text: str, is_message: bool, reply_markup=None):
    if is_message:
        await event.answer(text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        try:
            await event.message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            pass


def _subscription_keyboard(contest_id: int, missing) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ch in missing:
        url = None
        if ch.username:
            url = f"https://t.me/{ch.username.lstrip('@')}"
        elif ch.invite_link:
            url = ch.invite_link
        if url:
            kb.button(text=f"📢 {ch.title or ch.username or 'Канал'}", url=url)
    kb.button(text="🔄 Проверить подписку", callback_data=f"rz_join_{contest_id}")
    kb.adjust(1)
    return kb.as_markup()


async def _show_subscription_prompt(event, bot: Bot, contest_id: int, missing, is_message: bool):
    markup = _subscription_keyboard(contest_id, missing)
    text = "Для участия необходимо подписаться на все указанные каналы"
    if is_message:
        await event.answer(text, reply_markup=markup)
    else:
        try:
            await event.message.answer(text, reply_markup=markup)
        except Exception:
            pass


@router.callback_query(F.data.startswith("rz_leave_"))
async def on_leave_contest(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("rz_leave_", ""))
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        try:
            ok = await cs.leave_contest(session, contest_id=contest_id, user_id=callback.from_user.id)
        except cs.ContestClosedError:
            await callback.message.answer("❌ После завершения конкурса отмена участия невозможна.")
            return
        except cs.ContestError as e:
            await callback.message.answer(f"❌ {e}")
            return
    if ok:
        await callback.message.answer("✅ Ты вышел из конкурса. История участия сохранена.")
    else:
        await callback.message.answer("❌ Ты не участвуешь в этом конкурсе.")


# ===================== БИЛЕТЫ, КОДОВЫЕ СЛОВА, ЗАДАНИЯ, ПОДЕЛИТЬСЯ =====================

class CodeWordWizard(StatesGroup):
    waiting = State()


@router.callback_query(F.data == "rz_my_tickets")
async def on_my_tickets(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        entries = await cs.get_user_entries(session, callback.from_user.id, filter_="active")
        rows = []
        for entry, contest in entries[:15]:
            breakdown = await tickets.get_ticket_breakdown(session, contest.id, callback.from_user.id)
            rows.append((contest, int(entry.tickets), breakdown))
    if not rows:
        await callback.message.answer(
            "🎟 У тебя пока нет активных участий. Загляни в «🎁 Конкурсы»!"
        )
        return
    lines = ["🎟 <b>Мои билеты</b>\n"]
    for contest, count, breakdown in rows:
        parts = []
        if breakdown.get(tickets.REASON_BASE):
            parts.append(f"базовый {breakdown[tickets.REASON_BASE]}")
        if breakdown.get(tickets.REASON_REFERRAL):
            parts.append(f"за друзей {breakdown[tickets.REASON_REFERRAL]}")
        if breakdown.get(tickets.REASON_CODE_WORD):
            parts.append(f"за слова {breakdown[tickets.REASON_CODE_WORD]}")
        if breakdown.get(tickets.REASON_SPONSOR_TASK):
            parts.append(f"за задание {breakdown[tickets.REASON_SPONSOR_TASK]}")
        detail = f" ({', '.join(parts)})" if parts else ""
        lines.append(f"• <b>{contest.title}</b> — 🎟 {count}{detail}")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("rz_codeword_"))
async def on_code_word_prompt(callback: types.CallbackQuery, state: FSMContext):
    contest_id = int(callback.data.replace("rz_codeword_", ""))
    try:
        await callback.answer()
    except Exception:
        pass
    await state.set_state(CodeWordWizard.waiting)
    await state.update_data(contest_id=contest_id)
    await callback.message.answer(
        "🔑 Введи кодовое слово, которое ты нашёл в посте конкурса.\n\n"
        "Отправь его следующим сообщением.",
    )


@router.message(CodeWordWizard.waiting)
async def on_code_word_input(message: types.Message, state: FSMContext):
    if not message.from_user:
        return
    data = await state.get_data()
    contest_id = data.get("contest_id")
    if not contest_id:
        await state.clear()
        return
    text = (message.text or "").strip()
    async with session_scope() as session:
        result = await tickets.redeem_code_word(
            session, contest_id=contest_id, user_id=message.from_user.id, text=text
        )
        if result.status == "ok":
            from app.services import contest_referral_service as crefs

            await crefs.mark_conditions_met(session, contest_id=contest_id, user_id=message.from_user.id)
            await crefs.credit_referral_for_user(session, contest_id=contest_id, user_id=message.from_user.id)
    await state.clear()

    if result.status == "ok":
        await message.answer(
            f"✅ Кодовое слово принято! Ты получил <b>{result.tickets}</b> билет(ов). 🎟",
            parse_mode="HTML",
        )
    elif result.status == "already":
        await message.answer("Ты уже вводил это кодовое слово. Билеты начисляются один раз.")
    elif result.status == "disabled":
        await message.answer("В этом конкурсе кодовые слова не предусмотрены.")
    elif result.status == "no_entry":
        await message.answer("Сначала нужно участвовать в конкурсе — нажми «🎉 Участвовать».")
    else:
        await message.answer("❌ Такое кодовое слово не найдено. Проверь написание и попробуй снова.")


@router.callback_query(F.data.startswith("rz_task_"))
async def on_sponsor_task(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("rz_task_", ""))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if contest is None:
            await callback.answer("Конкурс не найден", show_alert=True)
            return
        settings = cset.read_settings(contest)
        if not settings.get("sponsor_tasks"):
            await callback.answer("Заданий в этом конкурсе нет", show_alert=True)
            return
        awarded = await tickets.submit_sponsor_task(
            session, contest_id=contest_id, user_id=callback.from_user.id
        )
        if awarded:
            from app.services import contest_referral_service as crefs

            await crefs.mark_conditions_met(session, contest_id=contest_id, user_id=callback.from_user.id)
            await crefs.credit_referral_for_user(session, contest_id=contest_id, user_id=callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass
    if awarded:
        await callback.message.answer(
            f"✅ Задание зачтено! Ты получил <b>{settings.get('sponsor_task_tickets', 1)}</b> билет(ов). 🎟",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer("Ты уже выполнил это задание. Билеты начисляются один раз.")


@router.callback_query(F.data.startswith("rz_share_"))
async def on_share(callback: types.CallbackQuery, bot: Bot):
    contest_id = int(callback.data.replace("rz_share_", ""))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        settings = cset.read_settings(contest) if contest else cset.default_settings()
        username = await _resolve_bot_username(bot, session)
        prize = contest.prize if contest else None
    try:
        await callback.answer()
    except Exception:
        pass
    if not username:
        await callback.message.answer("✉️ Ссылка временно недоступна.")
        return
    link = ref.build_referral_link(username, callback.from_user.id)
    share_text = settings.get("share_text") or cset.default_share_text(prize, link)
    if "{link}" in share_text:
        share_text = share_text.replace("{link}", link)
    elif link not in share_text:
        share_text = f"{share_text} {link}"
    kb = InlineKeyboardBuilder()
    from urllib.parse import quote
    kb.button(text="📤 Отправить друзьям", url=f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}")
    kb.adjust(1)
    await callback.message.answer(
        "📤 <b>Поделись конкурсом</b>\n\n"
        f"Готовый текст:\n<i>{share_text}</i>\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("rz_parts_"))
async def on_public_participants(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("rz_parts_", ""))
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if contest is None:
            await callback.message.answer("❌ Конкурс не найден.")
            return
        settings = cset.read_settings(contest)
        if not settings.get("show_participants"):
            await callback.message.answer("Организатор скрыл список участников.")
            return
        rows = await cs.list_participants_public(session, contest_id, limit=50)
        total = await cs.participants_count(session, contest_id)
    lines = [f"👥 <b>Участники «{contest.title}»</b> ({total})\n"]
    for i, (entry, player, invited) in enumerate(rows, start=1):
        name = (player.custom_nickname or player.first_name) if player else f"ID {entry.user_id}"
        uname = f" (@{player.username})" if player and player.username else ""
        extra = ""
        if settings.get("show_tickets"):
            extra = f" — 🎟 {int(entry.tickets)}"
        elif invited:
            extra = f" — 👥 {invited}"
        lines.append(f"{i}. {name}{uname}{extra}")
    if total > len(rows):
        lines.append(f"\n…и ещё {total - len(rows)}. Полный список — в Mini App.")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("rz_top_"))
async def on_public_leaderboard(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("rz_top_", ""))
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if contest is None:
            await callback.message.answer("❌ Конкурс не найден.")
            return
        settings = cset.read_settings(contest)
        if not settings.get("leaderboard"):
            await callback.message.answer("Рейтинг в этом конкурсе не ведётся.")
            return
        from app.services import leaderboard_service as lb
        top = await lb.get_top(session, contest_id, limit=int(settings.get("leaderboard_size", 10)))
    if not top:
        await callback.message.answer("🏅 Рейтинг пока пуст — приглашай друзей и выходи в лидеры!")
        return
    lines = [f"🏅 <b>Топ приглашающих «{contest.title}»</b>\n"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for place, (uid, count, player) in enumerate(top, start=1):
        name = (player.custom_nickname or player.first_name) if player else f"ID {uid}"
        uname = f" (@{player.username})" if player and player.username else ""
        mark = medals.get(place, f"{place}.")
        lines.append(f"{mark} {name}{uname} — 👥 {count}")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("rz_confirm_"))
async def on_confirm_prize(callback: types.CallbackQuery):
    contest_id = int(callback.data.replace("rz_confirm_", ""))
    async with session_scope() as session:
        ok = await ds.confirm_winner(session, contest_id=contest_id, user_id=callback.from_user.id)
    try:
        await callback.answer("Спасибо! Организатор увидит, что ты на связи." if ok else "Нечего подтверждать.", show_alert=not ok)
    except Exception:
        pass
    if ok:
        await callback.message.answer("✅ Ты подтвердил получение приза. Организатор свяжется с тобой!")



# ===================== ПРОФИЛЬ =====================

@router.message(Command("profile"))
@router.message(F.text.casefold().in_(["профиль", "!профиль", MENU_PROFILE.casefold()]))
async def cmd_profile(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        reg = await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name or "Игрок",
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        p = reg.player
        invited = await ref.count_invited(session, p.telegram_id)
        stats = await cs.get_user_stats(session, p.telegram_id)
        recent = await cs.get_user_entries(session, p.telegram_id)
        recent = recent[:5]

    name = p.custom_nickname or p.first_name or "Игрок"
    uname = f"@{p.username}" if p.username else "не указан"
    reg_date = p.registered_at.strftime("%d.%m.%Y") if p.registered_at else "—"
    lines = [
        f"👤 <b>Профиль {name}</b>\n",
        f"🆔 ID: <code>{p.telegram_id}</code>",
        f"📛 Username: {uname}",
        f"🗓 Регистрация: {reg_date}\n",
        f"👥 Приглашено друзей: <b>{invited}</b>",
        f"🎯 Активных участий: <b>{stats['active']}</b>",
        f"🏁 Завершённых участий: <b>{stats['finished']}</b>",
        f"🏆 Побед: <b>{stats['wins']}</b>",
    ]
    if recent:
        lines.append("\n📜 <b>Последние конкурсы:</b>")
        for entry, contest in recent:
            lines.append(f"• {contest.title}")
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Пригласить друзей", callback_data="rz_invite")
    kb.button(text="📜 История участия", callback_data="rz_history_all")
    kb.button(text="🏆 Мои победы", callback_data="rz_history_wins")
    kb.button(text="◀️ Назад", callback_data="rz_menu_back")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "rz_menu_back")
async def on_menu_back(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await callback.message.answer("Главное меню — выбери раздел на клавиатуре ниже.")
    except Exception:
        pass


# ===================== ПРИГЛАШЕНИЯ =====================

@router.message(Command("myref"))
@router.message(F.text.casefold().in_(["пригласить друзей", "!пригласить друзей", MENU_INVITE.casefold()]))
async def cmd_myref(message: types.Message, bot: Bot):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name or "Игрок",
            username=message.from_user.username,
        )
        username = await _resolve_bot_username(bot, session)
        admin = await _is_admin(session, message.from_user.id)
        count = await ref.count_invited(session, message.from_user.id)

    if not username:
        # Понятная ошибка только администратору; игрок видит общий текст.
        if admin:
            await message.answer(
                "⚠️ Не настроен username бота. Задайте BOT_USERNAME в настройках или "
                "убедитесь, что бот запущен и доступен через Telegram API.",
                parse_mode="HTML",
            )
        else:
            await message.answer("✉️ Раздел временно недоступен. Попробуйте позже.")
        logger.error("Не удалось сформировать реферальную ссылку: username бота не определён")
        return

    link = ref.build_referral_link(username, message.from_user.id)
    text = (
        "Приглашай друзей в Fable Randomizer! 👥\n\n"
        "Делись своей персональной ссылкой. Приглашения помогают увеличить твои "
        "шансы на победу в конкурсах!\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
        f"👥 Приглашено: <b>{count}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Скопировать ссылку", callback_data="rz_ref_copy")
    share_url = f"https://t.me/share/url?url={link}&text=Участвуй в конкурсах Fable Randomizer!"
    kb.button(text="📤 Поделиться ссылкой", url=share_url)
    kb.button(text="👥 Мои приглашения", callback_data="rz_ref_list")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "rz_ref_copy")
async def on_ref_copy(callback: types.CallbackQuery, bot: Bot):
    async with session_scope() as session:
        username = await _resolve_bot_username(bot, session)
    if not username:
        await callback.answer("Ссылка недоступна", show_alert=True)
        return
    link = ref.build_referral_link(username, callback.from_user.id)
    try:
        await callback.answer(f"Скопируй ссылку: {link}", show_alert=True)
    except Exception:
        pass


@router.callback_query(F.data == "rz_invite")
async def on_ref_invite_button(callback: types.CallbackQuery, bot: Bot):
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        username = await _resolve_bot_username(bot, session)
        count = await ref.count_invited(session, callback.from_user.id)
    if not username:
        await callback.message.answer("✉️ Раздел временно недоступен.")
        return
    link = ref.build_referral_link(username, callback.from_user.id)
    text = (
        "Приглашай друзей в Fable Randomizer! 👥\n\n"
        "Делись своей персональной ссылкой. Приглашения помогают увеличить твои "
        "шансы на победу в конкурсах!\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
        f"👥 Приглашено: <b>{count}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Скопировать ссылку", callback_data="rz_ref_copy")
    share_url = f"https://t.me/share/url?url={link}&text=Участвуй в конкурсах Fable Randomizer!"
    kb.button(text="📤 Поделиться ссылкой", url=share_url)
    kb.button(text="👥 Мои приглашения", callback_data="rz_ref_list")
    kb.adjust(1)
    await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "rz_ref_list")
async def on_ref_list(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    async with session_scope() as session:
        items = await ref.list_invited(session, callback.from_user.id)
    if not items:
        await callback.message.answer("👥 У тебя пока нет приглашённых друзей. Поделись своей ссылкой!")
        return
    lines = [f"👥 <b>Мои приглашения ({len(items)}):</b>\n"]
    for r, player in items:
        name = (player.custom_nickname or player.first_name) if player else "Игрок"
        uname = f"@{player.username}" if player and player.username else ""
        date = r.invited_at.strftime("%d.%m.%Y") if r.invited_at else "—"
        status = "✅ зарегистрирован" if player else "⏳ не подтверждён"
        lines.append(f"• {name} {uname} — {status} ({date})")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")


# ===================== ПОБЕДИТЕЛИ =====================

@router.message(Command("winners"))
@router.message(F.text.casefold().in_(["победители", "!победители", MENU_WINNERS.casefold()]))
async def cmd_winners(message: types.Message):
    await _send_winners(message)


@router.callback_query(F.data == "rz_show_winners")
async def on_show_winners_cb(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    await _send_winners(callback.message)


async def _send_winners(message, edit: bool = False):
    async with session_scope() as session:
        contests = await cs.list_contests(session, category="all", include_archived=True)
        finished = [c for c in contests if c.draw_done or await cs.winners_count(session, c.id) > 0]
        entries = []
        for c in finished:
            winners = await ds.get_latest_winners(session, c.id)
            if winners:
                participants = await cs.participants_count(session, c.id)
                entries.append((c, winners, participants))

    if not entries:
        text = "Пока нет завершённых конкурсов с результатами. Следи за обновлениями! 🏆"
        if edit:
            try:
                await message.edit_text(text)
                return
            except Exception:
                pass
        await message.answer(text)
        return

    lines = ["🏆 <b>Победители Fable Randomizer:</b>\n"]
    for c, winners, participants in entries[:15]:
        drawn_at = winners[0][0].drawn_at if winners and winners[0][0].drawn_at else None
        date = drawn_at.strftime("%d.%m.%Y") if drawn_at else "—"
        lines.append(
            f"🎁 <b>{c.title}</b>\n"
            f"   📅 Розыгрыш: {date} | 👥 Участников: {participants} | 🏆 Победителей: {len(winners)}"
        )
        for w, player in winners:
            name = (player.custom_nickname or player.first_name) if player else f"ID {w.user_id}"
            uname = f" (@{player.username})" if player and player.username else ""
            prize = f" — 🎁 {w.prize}" if w.prize else ""
            lines.append(f"      {w.place}. {name}{uname}{prize}")
    if edit:
        try:
            await message.edit_text("\n".join(lines), parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer("\n".join(lines), parse_mode="HTML")


# ===================== ИСТОРИЯ УЧАСТИЯ =====================

@router.message(Command("history"))
@router.message(F.text.casefold().in_(["история участия", "!история участия", MENU_HISTORY.casefold()]))
async def cmd_history(message: types.Message):
    await _send_history(message, filter_="all")


def _history_keyboard(active_filter: str):
    kb = InlineKeyboardBuilder()
    labels = {"all": "Все", "active": "Активные", "finished": "Завершённые", "wins": "Победы"}
    for key, label in labels.items():
        kb.button(text=("✅ " if active_filter == key else "") + label, callback_data=f"rz_history_{key}")
    kb.adjust(4)
    return kb


def _entry_status_label(entry, contest, is_winner: bool) -> str:
    status = cs.compute_status(contest)
    if entry.status == "left":
        return "🚪 Участие отменено"
    if is_winner:
        return "🏆 Ты победил"
    if status in (cs.STATUS_ACTIVE, cs.STATUS_SCHEDULED):
        return "🎯 Участвуешь"
    if status in (cs.STATUS_FINISHED, cs.STATUS_CANCELLED):
        return "😔 Ты не выиграл"
    return "🎯 Участвуешь"


async def _send_history(message, *, filter_: str, edit: bool = False):
    user_id = message.from_user.id if message.from_user else (message.chat.id)
    async with session_scope() as session:
        items = await cs.get_user_entries(session, user_id, filter_=filter_)
        rows = []
        for entry, contest in items[:20]:
            winner = await cs.is_winner(session, contest.id, user_id)
            rows.append((entry, contest, winner))

    if not rows:
        text = "📜 История участия пуста. Участвуй в конкурсах, чтобы она появилась!"
    else:
        lines = ["📜 <b>История участия</b>\n"]
        for entry, contest, winner in rows:
            date = entry.joined_at.strftime("%d.%m.%Y") if entry.joined_at else "—"
            lines.append(
                f"• <b>{contest.title}</b>\n"
                f"   📅 {date} | 🎁 {contest.prize or '—'} | {_entry_status_label(entry, contest, winner)}"
            )
        text = "\n".join(lines)

    kb = _history_keyboard(filter_)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("rz_history_"))
async def on_history_filter(callback: types.CallbackQuery):
    filter_ = callback.data.replace("rz_history_", "")
    try:
        await callback.answer()
    except Exception:
        pass
    await _send_history(callback.message, filter_=filter_, edit=True)


# ===================== ПОМОЩЬ =====================

HELP_TEXT = (
    "ℹ️ <b>Помощь Fable Randomizer</b>\n\n"
    "<b>Участие:</b>\n"
    "🎁 /contests — список конкурсов\n"
    "🎉 /join ID — участие в конкурсе\n"
    "👥 /myref — твоя реферальная ссылка\n"
    "👤 /profile — профиль\n"
    "📜 /history — история участия\n"
    "🏆 /winners — победители\n\n"
    "<b>Свои конкурсы:</b>\n"
    "➕ /createcontest — создать конкурс (для своего канала или без канала)\n"
    "📣 /mycontests — мои конкурсы и статистика\n\n"
    "Приглашай друзей по своей ссылке — это помогает увеличить шансы на победу!\n\n"
    "Если возникли проблемы, обратись в службу поддержки."
)


@router.message(Command("help"))
@router.message(F.text.casefold().in_(["помощь randomizer", "!помощь randomizer", MENU_HELP.casefold()]))
async def cmd_help(message: types.Message):
    async with session_scope() as session:
        support = await rset.get_support_link(session)
    kb = InlineKeyboardBuilder()
    if support:
        if support.startswith("http"):
            kb.button(text="🆘 Связаться с поддержкой", url=support)
        else:
            kb.button(text="🆘 Связаться с поддержкой", url=f"https://t.me/{support.lstrip('@')}")
    await message.answer(HELP_TEXT, reply_markup=kb.as_markup() if support else None, parse_mode="HTML")
