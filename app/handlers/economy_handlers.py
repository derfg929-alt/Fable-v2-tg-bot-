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
from app.services.mining_service import (
    get_or_create_mining,
    buy_computers,
    upgrade_mining_farm,
    collect_mining_profit,
    collect_mining_profit_detailed,
    switch_mining_coin,
    get_rigs_dict,
    calculate_effective_hashrate,
    get_farm_upgrade_cost,
    MINING_RIGS,
    SUPPORTED_MINING_COINS,
)
from app.services.business_service import (
    BUSINESSES,
    get_player_businesses,
    buy_business,
    upgrade_business,
    collect_business_profits,
    get_hourly_income,
    get_upgrade_cost,
)
from app.services.reputation_service import change_reputation, get_reputation
from app.services.activity_service import process_message_activity

router = Router(name="economy_router")


async def _extract_target_user_id(message: types.Message, session) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id
    # Проверка упоминания по username или ID
    text = message.text or ""
    match = re.search(r"@([a-zA-Z0-9_]+)", text)
    if match:
        uname = match.group(1).lower()
        from app.db.models import Player
        from sqlalchemy import select, func
        p = await session.scalar(select(Player).where(func.lower(Player.username) == uname))
        if p:
            return p.telegram_id

    # Проверка ID
    parts = text.split()
    for part in parts[1:]:
        if part.isdigit() and len(part) >= 6:
            return int(part)
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
        reg = await get_or_create_player(
            session,
            telegram_id=message.from_user.id,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            username=message.from_user.username,
        )
        player = reg.player

        if len(parts) == 1:
            status = await get_player_bank_account(session, user_id=message.from_user.id)
            wallet_bal = status.get("wallet_balance", player.wallet)
            bank_bal = status.get("bank_balance", player.bank_balance)
            rate = status.get("interest_rate", 0)
            rate_pct = float(rate) * 100
            resp = (
                f"🏦 <b>Банк Fable</b>\n\n"
                f"💰 На кошельке: <b>{wallet_bal}</b> монет\n"
                f"🏛 На вкладе: <b>{bank_bal}</b> монет\n"
                f"📈 Текущая ставка: <b>{rate_pct:.2f}%</b> в сутки\n"
                f"🕒 Начисление процентов происходит раз в сутки автоматически.\n\n"
                f"<i>Команды:</i>\n"
                f"• <code>банк положить [сумма|все]</code>\n"
                f"• <code>банк снять [сумма|все]</code>"
            )
            await message.reply(resp, parse_mode="HTML")
            return

        action = parts[1].casefold()
        if action in ("положить", "депозит") and len(parts) >= 3:
            try:
                amt_str = parts[2].casefold()
                if amt_str in ("все", "всё", "all"):
                    amt = player.wallet
                else:
                    amt = int(amt_str)
                res = await deposit_to_bank(session, user_id=message.from_user.id, amount=amt)
                await message.reply(
                    f"✅ Вы успешно положили <b>{amt}</b> монет в банк!\n"
                    f"💰 Кошелек: {res.wallet_balance} | 🏛 Вклад: {res.bank_balance}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ Ошибка: {e}")
            return

        if action in ("снять", "вывести") and len(parts) >= 3:
            try:
                amt_str = parts[2].casefold()
                if amt_str in ("все", "всё", "all"):
                    amt = player.bank_balance
                else:
                    amt = int(amt_str)
                res = await withdraw_from_bank(session, user_id=message.from_user.id, amount=amt)
                await message.reply(
                    f"✅ Вы успешно сняли <b>{amt}</b> монет с вклада!\n"
                    f"💰 Кошелек: {res.wallet_balance} | 🏛 Вклад: {res.bank_balance}",
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

    amount = None
    for p in reversed(parts):
        if p.isdigit():
            amount = int(p)
            break

    async with session_scope() as session:
        target_id = await _extract_target_user_id(message, session)
        if not target_id or not amount:
            await message.reply("Использование: <code>перевод @user 500</code> или ответом на сообщение: <code>перевод 500</code>", parse_mode="HTML")
            return

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


@router.message(F.text.casefold().startswith("бизнес"))
async def handle_businesses(message: types.Message):
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

        # Команда "бизнесы" или "бизнес" без аргументов
        if len(parts) == 1 or parts[1].casefold() in ("список", "лист"):
            user_businesses = await get_player_businesses(session, user_id=message.from_user.id)
            user_b_map = {b.business_id: b for b in user_businesses}

            lines = [
                "🏢 <b>БИЗНЕСЫ FABLE</b> (10 уровней)\n",
                "⏱ <i>Внимание: прибыль накапливается максимум за 4 часа! Если не собирать, бизнес перестает приносить доход, пока не заберете кассу.</i>\n",
            ]
            for b_id, cfg in BUSINESSES.items():
                if b_id in user_b_map:
                    ub = user_b_map[b_id]
                    inc = get_hourly_income(b_id, ub.level)
                    upg = get_upgrade_cost(b_id, ub.level) if ub.level < 10 else "MAX"
                    upg_str = f"{upg:,} монет".replace(",", " ") if isinstance(upg, int) else upg
                    lines.append(f"{cfg.icon} <b>#{b_id} {cfg.name}</b> [Ур. {ub.level}/10]\n   ├ Доход: <b>+{inc:,}/ч</b> (макс. за 4ч: +{inc * 4:,})\n   └ Улучшение: {upg_str}".replace(",", " "))
                else:
                    lines.append(f"{cfg.icon} <b>#{b_id} {cfg.name}</b> (Не куплен)\n   ├ Доход: +{cfg.base_income_hourly:,}/ч\n   └ Стоимость: <b>{cfg.base_cost:,}</b> монет".replace(",", " "))

            lines.append(
                "\n<i>Команды:</i>\n"
                "• <code>бизнес купить [номер]</code> — купить бизнес\n"
                "• <code>бизнес улучшить [номер]</code> — прокачать уровень\n"
                "• <code>бизнес собрать</code> — забрать накопленную прибыль (каждые 4ч)"
            )
            await message.reply("\n".join(lines), parse_mode="HTML")
            return

        cmd = parts[1].casefold()

        # Сбор прибыли
        if cmd in ("собрать", "прибыль", "доход", "collect"):
            try:
                coins, count = await collect_business_profits(session, user_id=message.from_user.id)
                if coins > 0:
                    await message.reply(
                        f"💰 <b>Прибыль с бизнесов собрана!</b>\n\n"
                        f"Получено: <b>+{coins:,}</b> монет от {count} предприятий!".replace(",", " "),
                        parse_mode="HTML",
                    )
                else:
                    await message.reply("⏳ Прибыль еще не накопилась или у вас нет активных бизнесов. Загляните чуть позже!")
            except Exception as e:
                await message.reply(f"❌ Ошибка: {e}")
            return

        # Покупка бизнеса
        if cmd in ("купить", "buy") and len(parts) >= 3 and parts[2].isdigit():
            b_id = int(parts[2])
            try:
                pb, cfg = await buy_business(session, user_id=message.from_user.id, business_id=b_id)
                inc = get_hourly_income(b_id, 1)
                await message.reply(
                    f"🎉 <b>Поздравляем с покупкой бизнеса!</b>\n\n"
                    f"{cfg.icon} <b>{cfg.name}</b> теперь принадлежит вам!\n"
                    f"Базовый доход: <b>+{inc:,}</b> монет/час\n"
                    f"Не забывайте улучшать его и собирать прибыль командой: <code>бизнес собрать</code>!".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        # Улучшение бизнеса
        if cmd in ("улучшить", "апгрейд", "upgrade") and len(parts) >= 3 and parts[2].isdigit():
            b_id = int(parts[2])
            try:
                pb, cost = await upgrade_business(session, user_id=message.from_user.id, business_id=b_id)
                cfg = BUSINESSES[b_id]
                new_inc = get_hourly_income(b_id, pb.level)
                await message.reply(
                    f"🚀 <b>Бизнес успешно улучшен!</b>\n\n"
                    f"{cfg.icon} <b>{cfg.name}</b> повышен до уровня <b>{pb.level}/10</b>!\n"
                    f"Новый доход: <b>+{new_inc:,}</b> монет/час\n"
                    f"Потрачено: <b>{cost:,}</b> монет.".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return


@router.message(F.text.casefold().in_(["ферма", "!ферма", "майнинг", "!майнинг"]))
@router.message(F.text.casefold().startswith("ферма"))
async def handle_mining(message: types.Message):
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

        mining = await get_or_create_mining(session, user_id=message.from_user.id)

        # Главное меню фермы
        if len(parts) == 1 or parts[0].casefold() in ("майнинг", "!майнинг"):
            rigs = get_rigs_dict(mining)
            total_comps = sum(rigs.values())
            active_coin = (mining.active_coin or "BTC").upper()
            hashrate = calculate_effective_hashrate(rigs, active_coin, mining.farm_level)
            upg_cost = get_farm_upgrade_cost(mining.farm_level) if mining.farm_level < 10 else "MAX"
            upg_str = f"{upg_cost:,} монет".replace(",", " ") if isinstance(upg_cost, int) else upg_cost

            rigs_lines = []
            for r_code, cfg in MINING_RIGS.items():
                count = rigs.get(r_code, 0)
                rigs_lines.append(f"{cfg.icon} <b>{cfg.name}</b>: <b>{count}</b> шт. (Добывает: {', '.join(cfg.allowed_coins)} | Цена: {cfg.price:,} монет)".replace(",", " "))

            resp = (
                f"⛏ <b>КРИПТО-МАЙНИНГ ФЕРМА FABLE</b>\n\n"
                f"🎯 Активная криптовалюта для майнинга: <b>{active_coin}</b>\n"
                f"⚡️ Уровень фермы: <b>{mining.farm_level} / 10</b> (Бонус: +{(mining.farm_level - 1)*25}%)\n"
                f"🔥 Общая вычислительная мощность: <b>{hashrate:,} GH/s</b>\n"
                f"🖥 Всего оборудования на ферме: <b>{total_comps}</b> шт.\n\n"
                f"<b>Ваши майнинг-компьютеры и риги:</b>\n"
                + "\n".join(rigs_lines)
                + f"\n\n🔧 Улучшение фермы до ур. {mining.farm_level + 1}: <b>{upg_str}</b>\n\n"
                f"<i>Команды:</i>\n"
                f"• <code>ферма монета [BTC|ETH|TON|SOL|DOGE]</code> — выбрать добываемую криптовалюту\n"
                f"• <code>ферма купить [cpu|gpu|asic|quantum] [кол-во]</code> — купить компьютеры/риги\n"
                f"• <code>ферма собрать</code> — забрать намайненную криптовалюту в <code>портфель</code>\n"
                f"• <code>ферма улучшить</code> — прокачать уровень фермы"
            )
            await message.reply(resp, parse_mode="HTML")
            return

        cmd = parts[1].casefold()

        # Выбор криптовалюты для майнинга
        if cmd in ("монета", "крипта", "coin") and len(parts) >= 3:
            chosen = parts[2].upper()
            try:
                active = await switch_mining_coin(session, user_id=message.from_user.id, coin=chosen)
                await message.reply(
                    f"🎯 <b>Криптовалюта для майнинга изменена!</b>\n\n"
                    f"Теперь ваши риги и компьютеры майнят: <b>{active}</b>\n"
                    f"Добытые активы поступают прямо в ваш <code>портфель</code>!",
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        # Сбор прибыли
        if cmd in ("собрать", "прибыль", "доход", "collect", "майнить"):
            try:
                res = await collect_mining_profit_detailed(session, user_id=message.from_user.id)
                if res.crypto_amount > 0 or res.coins > 0:
                    crypto_str = f"{res.crypto_amount:.8f}".rstrip("0").rstrip(".")
                    await message.reply(
                        f"⛏ <b>Майнинг успешно завершен!</b>\n\n"
                        f"Ваши <b>{res.total_computers}</b> компьютеров намайнили:\n"
                        f"🪙 <b>+{crypto_str} {res.crypto_coin}</b> (зачислено в портфель!)\n"
                        f"💰 <b>+{res.coins:,}</b> игровых монет!\n\n"
                        f"Посмотреть баланс активов: <code>портфель</code>".replace(",", " "),
                        parse_mode="HTML",
                    )
                else:
                    await message.reply("⏳ Компьютеры еще вычисляют блоки... Добыча накапливается! Загляните чуть позже.")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        # Покупка компьютеров
        if cmd in ("купить", "buy"):
            # Формат: "ферма купить cpu 2" или "ферма купить 2" или "ферма купить gpu"
            rig_type = "cpu"
            count = 1

            if len(parts) >= 3:
                p2 = parts[2].lower()
                if p2 in MINING_RIGS:
                    rig_type = p2
                    if len(parts) >= 4 and parts[3].isdigit():
                        count = int(parts[3])
                elif p2.isdigit():
                    count = int(p2)
                elif p2 in ("комп", "компы", "пк"):
                    if len(parts) >= 4 and parts[3].isdigit():
                        count = int(parts[3])

            try:
                m, cost = await buy_computers(session, user_id=message.from_user.id, count=count, rig_type=rig_type)
                cfg = MINING_RIGS[rig_type]
                await message.reply(
                    f"🖥 <b>Оборудование успешно установлено!</b>\n\n"
                    f"Куплено: <b>{count} шт. {cfg.name}</b> {cfg.icon} за <b>{cost:,}</b> монет.\n"
                    f"Специализация: <b>{', '.join(cfg.allowed_coins)}</b>\n"
                    f"Всего ПК на ферме: <b>{m.computers}</b> шт.\n\n"
                    f"Не забудьте настроить добычу: <code>ферма монета [тикер]</code>!".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        # Улучшение фермы
        if cmd in ("улучшить", "upgrade", "апгрейд"):
            try:
                m, cost = await upgrade_mining_farm(session, user_id=message.from_user.id)
                await message.reply(
                    f"⚡️ <b>Ферма успешно улучшена!</b>\n\n"
                    f"Новый уровень: <b>{m.farm_level} / 10</b>\n"
                    f"Потрачено: <b>{cost:,}</b> монет.\n"
                    f"Бонус к добыче повышен до: <b>+{(m.farm_level - 1)*25}%</b>!".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return


@router.message(F.text.casefold().startswith("реп"))
async def handle_reputation(message: types.Message):
    if not message.from_user:
        return

    text = (message.text or "").strip()
    direction = 1 if text.endswith("+") else (-1 if text.endswith("-") else 0)

    async with session_scope() as session:
        target_id = await _extract_target_user_id(message, session)
        if not target_id:
            await message.reply("Использование: <code>реп @user +</code> или <code>реп @user -</code>", parse_mode="HTML")
            return

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
