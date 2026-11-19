"""Команда /start, главное меню и реферальные диплинки."""
from __future__ import annotations

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject, CommandStart

from .. import database, keyboards
from ..utils import get_bot_username, ref_link

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    username = message.from_user.username
    await database.get_user(user_id, username)

    payload = command.args or ""
    if payload.startswith("ref_"):
        raw = payload[4:]
        if raw.isdigit():
            referrer_id = int(raw)
            if referrer_id != user_id:
                await database.set_pending_referral(user_id, referrer_id)

    await message.answer(
        "Привет! Это бот розыгрышей 🎁\n\n"
        "• «Активные розыгрыши» — список конкурсов, в которых можно участвовать.\n"
        "• «Мои билеты и рефералы» — твоя статистика и ссылка для друзей.\n"
        "• «Создать конкурс» — свой розыгрыш может запустить любой: для канала "
        "или без него (тогда участвовать смогут все).\n\n"
        "Чем больше друзей ты пригласишь — тем больше билетов и шансов на победу!",
        reply_markup=keyboards.main_menu(user_id),
    )


@router.message(F.text == keyboards.BTN_ACTIVE)
async def show_active(message: types.Message) -> None:
    contests = await database.list_contests()
    active = [c for c in contests.values() if c.get("status") == "active"]
    if not active:
        await message.answer("Сейчас нет активных розыгрышей.")
        return
    await message.answer("🎁 Активные розыгрыши:")
    for contest in active:
        text = (
            f"🎉 {contest.get('title')}\n"
            f"🎁 Приз: {contest.get('prize', '—')}\n"
            f"⏰ Итоги: {contest.get('ends_at', '—')}\n"
            f"👥 Участников: {len(contest.get('participants', []))}"
        )
        await message.answer(text, reply_markup=keyboards.join_keyboard(contest["id"]))


@router.message(F.text == keyboards.BTN_MY)
async def show_my(message: types.Message) -> None:
    user_id = message.from_user.id
    user = await database.get_user(user_id, message.from_user.username)
    bot_username = await get_bot_username(message.bot)
    link = ref_link(bot_username, user_id)

    refs = user.get("referrals", [])
    ref_lines = "\n".join(f"• id{r}" for r in refs[:20]) or "—"

    # Кнопка «Поделиться» показывается, если она включена в каком-либо конкурсе.
    contests = await database.list_contests()
    share_contest = next(
        (
            c["id"]
            for c in contests.values()
            if c.get("status") == "active" and c.get("settings", {}).get("share_button")
        ),
        None,
    )
    markup = keyboards.share_keyboard(share_contest) if share_contest else None
    await message.answer(
        f"🎟 Билетов: {user.get('tickets', 0)}\n"
        f"👥 Приглашено друзей: {len(refs)}\n"
        f"📋 Участий в конкурсах: {len(user.get('contests_participated', []))}\n\n"
        f"Твои рефералы:\n{ref_lines}\n\n"
        f"Твоя ссылка для друзей:\n{link}",
        reply_markup=markup,
    )


@router.message(Command("top"))
async def cmd_top(message: types.Message, command: CommandObject) -> None:
    """Рейтинг приглашающих: /top [contest_id] [N]."""
    args = (command.args or "").split()
    contest_id = args[0] if args else None
    contests = await database.list_contests()
    if contest_id is None:
        active = [c for c in contests.values() if c.get("status") == "active"]
        if not active:
            await message.answer("Сейчас нет активных розыгрышей.")
            return
        contest = active[0]
        contest_id = contest["id"]
    contest = contests.get(str(contest_id))
    if not contest:
        await message.answer("Конкурс не найден.")
        return
    size = int(contest.get("settings", {}).get("leaderboard_size", 10))
    if len(args) > 1 and args[1].isdigit():
        size = int(args[1])
    board = await database.get_leaderboard(contest_id, size)
    if not board:
        await message.answer("Пока никто не пригласил друзей.")
        return
    lines = [f"🏆 Рейтинг приглашающих — «{contest.get('title')}»", ""]
    for place, row in enumerate(board, start=1):
        name = f"@{row['username']}" if row.get("username") else f"id{row['user_id']}"
        lines.append(f"{place}. {name} — {row['invited']}")
    await message.answer("\n".join(lines))


@router.message(Command("participants"))
async def cmd_participants(message: types.Message, command: CommandObject) -> None:
    """Публичный список участников (постранично): /participants [contest_id] [page]."""
    args = (command.args or "").split()
    contest_id = args[0] if args else None
    contests = await database.list_contests()
    if contest_id is None:
        active = [c for c in contests.values() if c.get("status") == "active"]
        if not active:
            await message.answer("Сейчас нет активных розыгрышей.")
            return
        contest_id = active[0]["id"]
    contest = contests.get(str(contest_id))
    if not contest:
        await message.answer("Конкурс не найден.")
        return
    settings = contest.get("settings", {})
    if not settings.get("show_participants"):
        await message.answer("Список участников скрыт в настройках конкурса.")
        return
    participants = contest.get("participants", [])
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    per_page = 50
    start = (page - 1) * per_page
    chunk = participants[start : start + per_page]
    lines = [f"👥 Участвуют: {len(participants)} человек (страница {page})", ""]
    for uid in chunk:
        entry = f"• id{uid}"
        if settings.get("show_tickets"):
            tickets = await database.get_contest_tickets(int(uid), contest_id)
            entry += f" — {max(1, tickets)} билет(ов)"
        lines.append(entry)
    await message.answer("\n".join(lines))
