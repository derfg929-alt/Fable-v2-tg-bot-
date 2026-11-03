"""Обработчики экономики, банка, переводов, истории, репутации и профиля Fable.

Правила из ТЗ:
- Команды без слэша: "профиль", "банк", "банк положить 100", "банк снять 50", "перевод @user 100", "история", "реп @user +", "реп @user -", "реп @user".
- Начисление кристаллов/монет за активность в группах (с защитой от фарма, кулдаунами, без авто-мутов).
"""
from __future__ import annotations

import re
from aiogram import Router, types, F
from aiogram.enums import ChatType
from aiogram.filters import Command

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.bank_service import deposit_to_bank, withdraw_from_bank, get_player_bank_account
from app.services.transfer_service import transfer_coins_with_tax
from app.services.history_service import get_economy_history
from app.services.reputation_service import change_reputation, get_reputation
from app.services.activity_service import process_message_activity

router = Router(name="economy_router")


def _extract_target_user_id(message: types.Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id
    # Проверка упоминания по username
    match = re.search(r"@([a-zA-Z0-9_]+)", message.text or "")
    if match:
        # В реальном коде можно резолвить по БД
        pass
    return None


@router.message(F.text.casefold().in_(["профиль", "!профиль"]))
async def handle_profile(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        reg = await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        p = reg.player
        req_exp = p.level * 100
        to_next = max(0, req_exp - p.experience)

        text = (
            f"👤 <b>Профиль игрока {p.first_name}</b>\n\n"
            f"🆔 ID: <code>{p.telegram_id}</code>\n"
            f"⭐ Уровень: <b>{p.level}</b>\n"
            f"✨ Опыт: <b>{p.experience} / {req_exp}</b> (до след.: {to_next})\n"
            f"💰 Кошелек: <b>{p.wallet}</b> монет\n"
            f"🏦 Банк: <b>{p.bank_balance}</b> монет\n"
            f"📈 Биржевые акции: <b>{p.shares}</b> ед.\n"
            f"🤝 Репутация: <b>{p.reputation}</b>"
        )
        await message.reply(text, parse_mode="HTML")


@router.message(F.text.casefold().startswith("банк"))
async def handle_bank(message: types.Message):
    if not message.from_user:
        return
    text = (message.text or "").strip()
    parts = text.split()

    async with session_scope() as session:
        await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )

        if len(parts) == 1:
            account = await get_player_bank_account(session, user_id=message.from_user.id)
            rate_pct = account.interest_rate * 100
            resp = (
                f"🏦 <b>Банк Fable</b>\n\n"
                f"💰 На кошельке: <b>{account.wallet_balance}</b> монет\n"
                f"🏛 На вкладе: <b>{account.bank_balance}</b> монет\n"
                f"📈 Текущая ставка: <b>{rate_pct:.1f}%</b> в сутки\n"
                f"🕒 Начисление процентов происходит раз в сутки автоматически."
            )
            await message.reply(resp, parse_mode="HTML")
            return

        action = parts[1].casefold()
        if action in ("положить", "депозит") and len(parts) >= 3:
            try:
                amt = int(parts[2])
                res = await deposit_to_bank(session, user_id=message.from_user.id, amount=amt)
                await message.reply(
                    f"✅ Вы успешно положили <b>{amt}</b> монет в банк!\n"
                    f"Кошелек: {res.wallet_balance} | Вклад: {res.bank_balance}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ Ошибка: {e}")
            return

        if action in ("снять", "вывести") and len(parts) >= 3:
            try:
                amt = int(parts[2])
                res = await withdraw_from_bank(session, user_id=message.from_user.id, amount=amt)
                await message.reply(
                    f"✅ Вы успешно сняли <b>{amt}</b> монет с вклада!\n"
                    f"Кошелек: {res.wallet_balance} | Вклад: {res.bank_balance}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ Ошибка: {e}")
            return


@router.message(F.text.casefold().startswith("перевод"))
async def handle_transfer(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    target_id = _extract_target_user_id(message)

    # Если перевод по ответу на сообщение: "перевод 100"
    amount = None
    for p in reversed(parts):
        if p.isdigit():
            amount = int(p)
            break

    if not target_id or not amount:
        await message.reply("Использование: <code>перевод @user 500</code> или ответом на сообщение: <code>перевод 500</code>", parse_mode="HTML")
        return

    async with session_scope() as session:
        try:
            res = await transfer_coins_with_tax(
                session,
                sender_id=message.from_user.id,
                recipient_id=target_id,
                amount=amount,
            )
            await message.reply(
                f"💸 <b>Перевод выполнен успешно!</b>\n\n"
                f"Отправлено: {res.gross_amount} монет\n"
                f"Комиссия налога: {res.tax} монет\n"
                f"Получателю зачислено: {res.received_amount} монет\n"
                f"Ваш остаток: {res.sender_balance} монет",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ Ошибка перевода: {e}")


@router.message(F.text.casefold().in_(["история", "!история"]))
async def handle_history(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        page = await get_economy_history(session, viewer_id=message.from_user.id, limit=7)
        if not page.items:
            await message.reply("📜 История операций пуста.")
            return

        lines = ["📜 <b>Последние финансовые операции:</b>\n"]
        for item in page.items:
            sign = "+" if item.recipient_id == message.from_user.id else "-"
            lines.append(
                f"• [{item.operation_type}] {sign}{item.amount} монет | {item.reason or ''}"
            )
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("реп"))
async def handle_reputation(message: types.Message):
    if not message.from_user:
        return
    target_id = _extract_target_user_id(message)
    if not target_id:
        await message.reply("Использование: <code>реп @user +</code> или <code>реп @user -</code>", parse_mode="HTML")
        return

    text = (message.text or "").strip()
    direction = 1 if text.endswith("+") else (-1 if text.endswith("-") else 0)

    async with session_scope() as session:
        if direction == 0:
            rep = await get_reputation(session, user_id=target_id)
            await message.reply(f"🤝 Репутация игрока: <b>{rep}</b>", parse_mode="HTML")
            return

        try:
            res = await change_reputation(
                session,
                actor_id=message.from_user.id,
                target_id=target_id,
                direction=direction,
                chat_id=message.chat.id,
            )
            sign = "+1" if direction > 0 else "-1"
            await message.reply(
                f"🤝 Вы изменили репутацию игрока на {sign}!\nТекущая репутация: <b>{res.reputation}</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ Ошибка: {e}")
