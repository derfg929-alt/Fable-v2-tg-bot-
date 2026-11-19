"""Жеребьёвка, публикация итогов и передача приза запасным победителям."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Router, types
from aiogram.filters import Command

from .. import database
from ..draw import draw_winners

logger = logging.getLogger("fable_random_bot")

router = Router(name="draw")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def finish_contest(bot, contest_id: str) -> dict | None:
    """Провести жеребьёвку, сохранить итоги и опубликовать в канал."""
    data = await database.load_data()
    contest = data["contests"].get(str(contest_id))
    if not contest or contest.get("status") != "active":
        return None

    result = draw_winners(
        contest,
        data["users"],
        int(contest.get("winners_count", 1)),
        int(contest.get("settings", {}).get("reserve_count", 0)),
    )
    contest = await database.set_winners(
        contest_id, result["winners"], result["reserve"], result["transparency"]
    )
    if contest is None:
        return None
    await database.update_contest(contest_id, draw_at=datetime.now().isoformat(timespec="seconds"))
    await _publish_results(bot, contest)
    return contest


async def _publish_results(bot, contest: dict) -> None:
    channel_id = contest.get("channel_id")
    winners = contest.get("winners", [])
    reserve = contest.get("reserve_winners", [])
    tr = contest.get("transparency", {})
    lines = [f"🏆 Итоги розыгрыша «{contest.get('title')}»", ""]
    lines.append("Победители: " + (", ".join(f"id{w}" for w in winners) or "—"))
    if reserve:
        lines.append("Запасные: " + ", ".join(f"id{w}" for w in reserve))
    lines.append("")
    lines.append(f"Всего слотов (билетов): {tr.get('total_tickets', '—')}")
    lines.append(f"SHA-256: {tr.get('sha256', '—')}")
    lines.append(f"Соль: {tr.get('salt', '—')}")
    lines.append("")
    lines.append("Победителям: свяжитесь с организатором конкурса.")
    text = "\n".join(lines)
    if channel_id:
        try:
            await bot.send_message(chat_id=channel_id, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось опубликовать итоги %s: %s", contest.get("id"), exc)
    owner_id = contest.get("owner_id")
    if owner_id:
        # Конкурс мог быть создан без канала — организатор всё равно должен узнать итоги.
        try:
            await bot.send_message(chat_id=int(owner_id), text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось уведомить организатора %s: %s", owner_id, exc)
    for winner in winners:
        try:
            await bot.send_message(
                chat_id=int(winner),
                text=f"🎉 Ты выиграл в конкурсе «{contest.get('title')}»! Свяжись с организатором конкурса.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось уведомить победителя %s: %s", winner, exc)


async def check_and_finish(bot) -> None:
    """Завершить конкурсы, у которых наступила дата окончания."""
    now = datetime.now()
    data = await database.load_data()
    for contest in data["contests"].values():
        if contest.get("status") != "active":
            continue
        ends_at = _parse_dt(contest.get("ends_at"))
        if ends_at and ends_at <= now:
            await finish_contest(bot, contest["id"])


async def check_reserve_timeouts(bot) -> None:
    """Если победитель не откликнулся за reserve_timeout_hours — передать приз запасному."""
    now = datetime.now()
    data = await database.load_data()
    for contest in data["contests"].values():
        if contest.get("status") != "finished" or contest.get("reserve_transferred"):
            continue
        winners = contest.get("winners", [])
        reserve = contest.get("reserve_winners", [])
        if not winners or not reserve:
            continue
        draw_at = _parse_dt(contest.get("draw_at"))
        timeout_h = int(contest.get("settings", {}).get("reserve_timeout_hours", 24))
        if not draw_at or now - draw_at < timedelta(hours=timeout_h):
            continue
        confirmed = set(contest.get("confirmed_winners", []))
        unconfirmed = [w for w in winners if w not in confirmed]
        if not unconfirmed:
            continue
        replacement = reserve[0]
        new_winners = [w for w in winners if w in confirmed] + [replacement]
        await database.update_contest(
            contest["id"],
            winners=new_winners,
            reserve_winners=reserve[1:],
            reserve_transferred=True,
        )
        if contest.get("channel_id"):
            try:
                await bot.send_message(
                    chat_id=contest["channel_id"],
                    text=(
                        f"⏳ Победитель не откликнулся. Приз в конкурсе "
                        f"«{contest.get('title')}» переходит к запасному: id{replacement}."
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось опубликовать передачу приза: %s", exc)


@router.message(Command("confirm"))
async def cmd_confirm(message: types.Message) -> None:
    user_id = str(message.from_user.id)
    data = await database.load_data()
    changed = 0
    for contest in data["contests"].values():
        if contest.get("status") == "finished" and user_id in contest.get("winners", []):
            confirmed = set(contest.get("confirmed_winners", []))
            confirmed.add(user_id)
            await database.update_contest(contest["id"], confirmed_winners=sorted(confirmed))
            changed += 1
    if changed:
        await message.answer("✅ Отклик зафиксирован. Организатор конкурса свяжется с тобой.")
    else:
        await message.answer("Нет активных выигрышей, требующих подтверждения.")
