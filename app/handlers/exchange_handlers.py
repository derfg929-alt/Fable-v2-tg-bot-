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
from app.services.crypto_stock_service import (
    MARKET_ASSETS,
    fetch_real_prices,
    buy_asset,
    sell_asset,
    get_player_portfolio,
    get_asset_price,
)

router = Router(name="exchange_router")


@router.message(F.text.casefold().in_(["портфель", "!портфель"]))
async def handle_portfolio(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name,
        )
        items = await get_player_portfolio(session, user_id=message.from_user.id)
        if not items:
            await message.reply(
                "💼 <b>Ваш инвестиционный портфель пуст!</b>\n\n"
                "Купите криптовалюту или акции на бирже:\n"
                "• <code>биржа</code> — список котировок\n"
                "• <code>биржа купить btc 0.1</code>\n"
                "• <code>биржа купить aapl 2</code>",
                parse_mode="HTML",
            )
            return

        lines = ["💼 <b>ВАШ ИНВЕСТИЦИОННЫЙ ПОРТФЕЛЬ:</b>\n"]
        total_val = Decimal("0")
        for info, amt, val in items:
            total_val += val
            amt_str = f"{amt:.4f}".rstrip("0").rstrip(".")
            val_str = f"{int(val):,}".replace(",", " ")
            lines.append(f"{info.icon} <b>{info.ticker}</b> ({info.name}): <b>{amt_str}</b> шт. ≈ {val_str} монет")

        tot_str = f"{int(total_val):,}".replace(",", " ")
        lines.append(f"\n💰 <b>Общая стоимость активов:</b> ≈ <b>{tot_str}</b> монет")
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().in_(["фондовый рынок", "!фондовый рынок", "рынок", "!рынок"]))
@router.message(F.text.casefold().startswith("фондовый рынок"))
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

        # Если команда просто "биржа", "фондовый рынок", "рынок"
        first_phrase = text.casefold()
        if (
            len(parts) == 1
            or first_phrase in ("фондовый рынок", "!фондовый рынок", "рынок", "!рынок")
            or (parts[0].casefold() == "фондовый" and len(parts) == 2 and parts[1].casefold() == "рынок")
        ):
            prices = await fetch_real_prices()

            crypto_lines = ["🪙 <b>КРИПТОВАЛЮТА И КРИПТОГРАФИЯ (Реальный курс 1:1):</b>"]
            stock_lines = ["📈 <b>ФОНДОВЫЙ РЫНОК / АКЦИИ КОМПАНИЙ:</b>"]

            for ticker, info in MARKET_ASSETS.items():
                price = prices.get(ticker, info.default_price)
                price_str = f"{price:,.2f}".replace(",", " ") if price < 1000 else f"{int(price):,}".replace(",", " ")
                line = f"{info.icon} <b>{ticker}</b> ({info.name}): <b>{price_str}</b> монет"
                if info.category == "crypto":
                    crypto_lines.append(line)
                else:
                    stock_lines.append(line)

            resp = (
                "📊 <b>ФОНДОВЫЙ РЫНОК И БИРЖА КРИПТОВАЛЮТ FABLE</b>\n\n"
                + "\n".join(crypto_lines)
                + "\n\n"
                + "\n".join(stock_lines)
                + "\n\n"
                "<i>Вы можете купить или продать любые активы в любом количестве:</i>\n"
                "• <code>биржа купить [тикер] [любое кол-во]</code> (напр. <code>биржа купить btc 0.05</code> или <code>купить btc 1</code>)\n"
                "• <code>биржа продать [тикер] [любое кол-во]</code> (напр. <code>биржа продать btc 0.05</code>)\n"
                "• <code>портфель</code> — посмотреть купленную криптовалюту и акции\n"
                "• <code>биржа стакан</code> — классический биржевой стакан"
            )
            await message.reply(resp, parse_mode="HTML")
            return

        cmd = parts[1].casefold()

        # Классический стакан акций Fable
        if cmd == "стакан":
            book = await get_order_book(session, depth=3)
            bids_str = "\n".join([f"  • {b.price:.2f} — {b.total_quantity} шт." for b in book.bids]) or "  (нет заявок)"
            asks_str = "\n".join([f"  • {a.price:.2f} — {a.total_quantity} шт." for a in book.asks]) or "  (нет заявок)"
            best_b = f"{book.best_bid:.2f}" if book.best_bid else "—"
            best_a = f"{book.best_ask:.2f}" if book.best_ask else "—"
            resp = (
                f"📊 <b>СТАКАН БИРЖЕВЫХ АКЦИЙ FABLE</b>\n\n"
                f"Текущий курс: <b>{book.last_price:.2f}</b>\n"
                f"Покупка: <b>{best_b}</b> | Продажа: <b>{best_a}</b>\n\n"
                f"<b>Покупатели:</b>\n{bids_str}\n"
                f"<b>Продавцы:</b>\n{asks_str}"
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

        # Покупка / продажа крипты и акций: "биржа купить btc 0.5" или "фондовый рынок купить btc 0.5"
        # Также поддерживаем "купить [тикер] [кол-во]"
        action_idx = -1
        for i, p in enumerate(parts):
            if p.casefold() in ("купить", "продать"):
                action_idx = i
                break

        if action_idx != -1 and len(parts) >= action_idx + 2:
            trade_action = parts[action_idx].casefold()
            second_arg = parts[action_idx + 1].upper()
            if second_arg in MARKET_ASSETS:
                qty_str = parts[action_idx + 2].replace(",", ".") if len(parts) >= action_idx + 3 else "1"
                try:
                    qty = Decimal(qty_str)
                    if qty <= 0:
                        raise ValueError("Количество должно быть больше нуля")
                    if trade_action == "купить":
                        price, cost = await buy_asset(session, user_id=message.from_user.id, ticker=second_arg, quantity=qty)
                        info = MARKET_ASSETS[second_arg]
                        await message.reply(
                            f"🎉 <b>Успешная покупка активов!</b>\n\n"
                            f"Куплено: <b>{qty} {info.ticker}</b> ({info.name})\n"
                            f"Курс: <b>{price:,.2f}</b> монет\n"
                            f"Списано: <b>{cost:,}</b> монет\n\n"
                            f"Просмотреть активы: <code>портфель</code>".replace(",", " "),
                            parse_mode="HTML",
                        )
                    else:
                        price, rev = await sell_asset(session, user_id=message.from_user.id, ticker=second_arg, quantity=qty)
                        info = MARKET_ASSETS[second_arg]
                        await message.reply(
                            f"💰 <b>Успешная продажа активов!</b>\n\n"
                            f"Продано: <b>{qty} {info.ticker}</b> ({info.name})\n"
                            f"Курс: <b>{price:,.2f}</b> монет\n"
                            f"Зачислено: <b>+{rev:,}</b> монет".replace(",", " "),
                            parse_mode="HTML",
                        )
                except Exception as e:
                    await message.reply(f"❌ Ошибка: {e}")
                return

            if parts[action_idx + 1].isdigit():
                qty = int(parts[action_idx + 1])
                side = "buy" if trade_action == "купить" else "sell"
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
