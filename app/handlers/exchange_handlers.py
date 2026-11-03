"""Обработчики биржи Fable:
- "биржа"
- "биржа купить 100"
- "биржа продать 100"
- "биржа заявка купить 100 1.2"
- "биржа заявка продать 100 1.1"
- "биржа отмена"
- "биржа график"
"""
from __future__ import annotations

import re
from decimal import Decimal
from aiogram import Router, types, F
from aiogram.types import BufferedInputFile

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.exchange_service import (
    get_order_book,
    execute_market_order,
    create_limit_order,
    cancel_order,
    get_user_active_orders,
)
from app.services.market_chart import generate_price_chart_bytes

router = Router(name="exchange_router")


@router.message(F.text.casefold().startswith("биржа"))
async def handle_exchange(message: types.Message):
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
            book = await get_order_book(session, depth=3)
            bids_str = "\n".join([f"  • {b.price:.2f} — {b.total_quantity} шт." for b in book.bids]) or "  (нет заявок)"
            asks_str = "\n".join([f"  • {a.price:.2f} — {a.total_quantity} шт." for a in book.asks]) or "  (нет заявок)"

            best_b = f"{book.best_bid:.2f}" if book.best_bid else "—"
            best_a = f"{book.best_ask:.2f}" if book.best_ask else "—"
            spread_str = f"{book.spread:.2f}" if book.spread else "—"

            resp = (
                f"📊 <b>БИРЖА FABLE</b>\n\n"
                f"Текущий курс: <b>{book.last_price:.2f}</b>\n"
                f"Покупка: <b>{best_b}</b> | Продажа: <b>{best_a}</b>\n"
                f"Спред: <b>{spread_str}</b>\n"
                f"Изменение за сутки: <b>{book.price_change_24h_percent:+.1f}%</b>\n"
                f"Объём за сутки: <b>{book.daily_volume}</b> ед. ({book.daily_trade_count} сделок)\n"
                f"Комиссия: <b>1%</b>\n\n"
                f"📈 <b>Стакан заявок:</b>\n"
                f"<b>Покупатели (Bids):</b>\n{bids_str}\n\n"
                f"<b>Продавцы (Asks):</b>\n{asks_str}\n\n"
                f"<i>Команды:</i>\n"
                f"<code>биржа купить 100</code>\n"
                f"<code>биржа продать 100</code>\n"
                f"<code>биржа заявка купить 100 1.25</code>\n"
                f"<code>биржа график</code> | <code>биржа отмена</code>"
            )
            await message.reply(resp, parse_mode="HTML")
            return

        cmd = parts[1].casefold()

        # График
        if cmd in ("график", "история", "спрос"):
            chart_bytes = await generate_price_chart_bytes(session)
            photo = BufferedInputFile(chart_bytes, filename="chart.png")
            await message.reply_photo(photo, caption="📈 График торгов биржи Fable")
            return

        # Отмена заявки
        if cmd == "отмена":
            orders = await get_user_active_orders(session, user_id=message.from_user.id)
            if not orders:
                await message.reply("У вас нет активных биржевых заявок.")
                return
            target_order = orders[0]
            cancelled = await cancel_order(session, order_id=target_order.id, user_id=message.from_user.id)
            await message.reply(f"✅ Заявка #{cancelled.id} ({cancelled.side} {cancelled.remaining_quantity} шт.) отменена, неиспользованный резерв возвращен.")
            return

        # Рыночная покупка / продажа
        if cmd in ("купить", "продать") and len(parts) >= 3 and parts[2].isdigit():
            qty = int(parts[2])
            side = "buy" if cmd == "купить" else "sell"
            try:
                summary = await execute_market_order(session, user_id=message.from_user.id, side=side, quantity=qty)
                act = "куплено" if side == "buy" else "продано"
                await message.reply(
                    f"✅ <b>Рыночный ордер исполнен!</b>\n\n"
                    f"Успешно {act}: <b>{summary.filled_quantity} / {qty}</b> шт.\n"
                    f"Сумма: <b>{summary.total_cost_coins}</b> монет\n"
                    f"Комиссия биржи (1%): <b>{summary.total_fee_coins}</b> монет",
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ Ошибка сделки: {e}")
            return

        # Лимитная заявка: "биржа заявка купить 100 1.25"
        if cmd == "заявка" and len(parts) >= 5:
            side_raw = parts[2].casefold()
            side = "buy" if side_raw in ("купить", "buy") else ("sell" if side_raw in ("продать", "sell") else None)
            if not side or not parts[3].isdigit():
                await message.reply("Использование: <code>биржа заявка купить 100 1.25</code>", parse_mode="HTML")
                return
            qty = int(parts[3])
            price = parts[4].replace(",", ".")
            try:
                order, summary = await create_limit_order(
                    session,
                    user_id=message.from_user.id,
                    side=side,
                    quantity=qty,
                    price=price,
                )
                msg = f"✅ Лимитная заявка #{order.id} на {side} {qty} шт. по цене {price} создана!"
                if summary.filled_quantity > 0:
                    msg += f"\nИсполнено сразу: {summary.filled_quantity} шт. (остаток: {order.remaining_quantity})"
                await message.reply(msg)
            except Exception as e:
                await message.reply(f"❌ Ошибка: {e}")
            return
