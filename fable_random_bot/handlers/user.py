"""Участие в конкурсах: подписки, билеты, рефералы, кодовые слова."""
from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.types import CallbackQuery

from .. import database, keyboards
from ..utils import (
    SubscriptionChecker,
    display_name,
    get_bot_username,
    missing_subscriptions,
    ref_link,
)

logger = logging.getLogger("fable_random_bot")

router = Router(name="user")

# Заполняется в main.py при старте; в тестах остаётся None.
checker: SubscriptionChecker | None = None


def _checker() -> SubscriptionChecker:
    global checker
    if checker is None:
        checker = SubscriptionChecker(bot=None)  # type: ignore[arg-type]
    return checker


async def _grant_referral_bonus(contest: dict, referred_id: int) -> int | None:
    """Засчитать реферал, если пользователь пришёл по ссылке. Возвращает id реферера."""
    referrer_id = await database.pop_pending_referral(referred_id)
    if referrer_id is None:
        return None
    credited = await database.add_referral(referrer_id, referred_id, contest["id"])
    if not credited:
        return None
    tickets = int(contest.get("settings", {}).get("tickets_per_ref", 5))
    if tickets:
        await database.add_contest_tickets(referrer_id, contest["id"], tickets)
    return referrer_id


@router.callback_query(F.data.startswith("join_"))
async def on_join(callback: CallbackQuery) -> None:
    contest_id = callback.data.split("_", 1)[1]
    contest = await database.get_contest(contest_id)
    if not contest or contest.get("status") != "active":
        await callback.answer("Конкурс недоступен", show_alert=True)
        return

    user_id = callback.from_user.id
    username = callback.from_user.username
    await database.get_user(user_id, username)

    if str(user_id) in contest.get("participants", []):
        await callback.answer("Ты уже участвуешь", show_alert=True)
        return

    await callback.answer("Проверяю подписку…")

    missing = await missing_subscriptions(_checker(), contest, user_id)
    if missing:
        text = (
            "Для участия нужно подписаться на:\n"
            + "\n".join(f"• {c}" for c in missing)
            + "\n\nПодпишись и нажми «Проверить снова»."
        )
        await callback.message.answer(
            text, reply_markup=keyboards.join_failed_keyboard(contest_id, missing)
        )
        return

    added = await database.add_participant(contest_id, user_id)
    if not added:
        await callback.answer("Ты уже участвуешь", show_alert=True)
        return
    await database.add_contest_tickets(user_id, contest_id, 1)

    referrer_id = await _grant_referral_bonus(contest, user_id)
    bot_username = await get_bot_username(callback.bot)
    link = ref_link(bot_username, user_id)

    settings = contest.get("settings", {})
    text = f"Ты участвуешь! 🎉\nТвоя ссылка для друзей:\n{link}"
    if referrer_id:
        text += "\n\n✅ Реферал засчитан пригласившему."
    if settings.get("code_words"):
        text += "\n\nОтправь кодовое слово, чтобы получить дополнительные билеты."
    if settings.get("sponsor_tasks") and settings.get("sponsor_task_text"):
        text += f"\n\n📌 Задание спонсора:\n{settings['sponsor_task_text']}"

    markup = keyboards.share_keyboard(contest_id) if settings.get("share_button") else None
    await callback.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("check_"))
async def on_check(callback: CallbackQuery) -> None:
    contest_id = callback.data.split("_", 1)[1]
    contest = await database.get_contest(contest_id)
    if not contest or contest.get("status") != "active":
        await callback.answer("Конкурс недоступен", show_alert=True)
        return
    await callback.answer("Проверяю…")
    missing = await missing_subscriptions(_checker(), contest, callback.from_user.id)
    if missing:
        await callback.message.answer(
            "Подписка ещё не выполнена: " + ", ".join(missing),
            reply_markup=keyboards.join_failed_keyboard(contest_id, missing),
        )
    else:
        await callback.message.answer(
            "Подписка выполнена! Нажми «Участвовать».",
            reply_markup=keyboards.join_keyboard(contest_id),
        )


@router.callback_query(F.data.startswith("share_"))
async def on_share(callback: CallbackQuery) -> None:
    contest_id = callback.data.split("_", 1)[1]
    contest = await database.get_contest(contest_id)
    bot_username = await get_bot_username(callback.bot)
    link = ref_link(bot_username, callback.from_user.id)
    share_text = (contest or {}).get("settings", {}).get(
        "share_text", "Участвую в розыгрыше! Присоединяйся:"
    )
    await callback.message.answer(f"{share_text}\n{link}")
    await callback.answer("Скопируй и отправь друзьям")


@router.message(F.text.regexp(r"^[^\s/].*"))
async def on_text(message: types.Message) -> None:
    """Кодовое слово: проверяем против активных конкурсов, где участник есть."""
    user_id = message.from_user.id
    word = (message.text or "").strip().upper()
    if not word:
        return
    contests = await database.list_contests()
    for contest in contests.values():
        if contest.get("status") != "active":
            continue
        if str(user_id) not in contest.get("participants", []):
            continue
        for entry in contest.get("settings", {}).get("code_words", []) or []:
            if isinstance(entry, dict):
                code = str(entry.get("word", "")).upper()
                tickets = int(entry.get("tickets", 1))
            else:
                code = str(entry).upper()
                tickets = int(contest.get("settings", {}).get("code_word_tickets", 1))
            if word != code:
                continue
            fresh = await database.register_code_word(contest["id"], user_id, code)
            if not fresh:
                await message.answer("Это кодовое слово ты уже использовал.")
                return
            await database.add_contest_tickets(user_id, contest["id"], tickets)
            await message.answer(
                f"✅ Кодовое слово принято! +{tickets} билет(ов) в конкурсе «{contest['title']}»."
            )
            return
