"""Обработчики экономики, банка, переводов, истории, репутации и профиля Fable.

Правила из ТЗ:
- Команды без слэша: "профиль", "банк", "банк положить 100", "банк снять 50", "перевод @user 100", "история", "реп @user +", "реп @user -", "реп @user".
- Начисление кристаллов/монет за активность в группах (с защитой от фарма, кулдаунами, без авто-мутов).
"""
from __future__ import annotations

from decimal import Decimal
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
from app.services.mine_service import (
    get_or_create_mine,
    dig_in_mine,
    upgrade_pickaxe,
    smelt_ores,
    ORE_PRICES,
)
from app.services.inventory_service import (
    get_full_player_inventory,
    sell_inventory_item,
    sell_all_inventory,
    ITEM_DISPLAY_INFO,
    resolve_item_key,
)
from app.services.job_service import perform_main_job, perform_side_job, SIDE_JOBS
from app.services.rating_service import buy_rating, sell_rating
from app.services.case_service import open_case, CASES
from app.services.shop_service import (
    buy_property,
    buy_car,
    buy_shop_item,
    get_player_properties,
    get_player_cars,
    get_player_shop_items,
    PROPERTIES,
    CARS,
    SHOP_ITEMS,
)
from app.services.cosmodrome_service import (
    ROCKETS,
    get_or_create_cosmodrome,
    get_rockets_dict,
    calculate_hourly_income,
    buy_rocket,
    upgrade_cosmodrome,
    collect_cosmodrome_profits,
    launch_rocket_mission,
    get_cosmodrome_upgrade_cost,
)
from app.services.gear_service import (
    GEAR_ITEMS,
    get_or_create_gear,
    upgrade_player_gear,
    get_gear_upgrade_cost,
    get_gear_bonus_percent,
)

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


# ===================== БАЛАНС И ИНВЕНТАРЬ =====================

@router.message(F.text.casefold().in_(["баланс", "!баланс"]))
async def handle_balance(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        p = reg.player
        mine = await get_or_create_mine(session, user_id=p.telegram_id)

        from app.db.models import PlayerAsset
        assets = await session.scalars(select(PlayerAsset).where(PlayerAsset.user_id == p.telegram_id))
        crypto_lines = []
        for a in assets:
            if a.amount > 0:
                crypto_lines.append(f"• {a.ticker}: <b>{a.amount:.6f}</b>")
        crypto_str = "\n".join(crypto_lines) if crypto_lines else "• Нет криптовалюты"

        resp = (
            f"💰 <b>ФИНАНСОВЫЙ БАЛАНС</b>\n\n"
            f"💵 Наличные: <b>{p.wallet:,}</b> монет\n"
            f"🏛 В банке: <b>{p.bank_balance:,}</b> монет\n"
            f"⭐ Рейтинг: <b>{p.rating:,}</b> очков\n\n"
            f"🪙 <b>Криптовалюта:</b>\n{crypto_str}\n\n"
            f"⛏ <b>Запасы руды в шахте:</b>\n"
            f"• 🪨 Уголь: <b>{mine.coal:,}</b> шт.\n"
            f"• 🔩 Железо: <b>{mine.iron:,}</b> шт.\n"
            f"• 🥉 Медь: <b>{mine.copper:,}</b> шт.\n"
            f"• 🥈 Серебро: <b>{mine.silver:,}</b> шт.\n"
            f"• 🥇 Золото: <b>{mine.gold:,}</b> шт.\n"
            f"• 💎 Металлические слитки: <b>{mine.processed_metals:,}</b> шт.\n\n"
            f"💡 Чтобы продать добытую руду, напишите команду: <code>инвентарь</code>".replace(",", " ")
        )
        await message.reply(resp, parse_mode="HTML")


def _build_inventory_keyboard(inv_data: dict):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()

    # Кнопки для руды из шахты
    for item in inv_data["mine_items"]:
        builder.button(
            text=f"{item.icon} {item.title}: продать все ({item.quantity} шт. | +{item.total_price:,}м)".replace(",", " "),
            callback_data=f"inv_sell_{item.key}_all",
        )

    # Кнопки для рыбы и охоты
    for item in inv_data["farm_loot"]:
        builder.button(
            text=f"{item.icon} {item.title}: продать все ({item.quantity} шт. | +{item.total_price:,}м)".replace(",", " "),
            callback_data=f"inv_sell_{item.key}_all",
        )

    # Кнопка для полной продажи
    if inv_data["total_value"] > 0:
        builder.button(
            text=f"💰 Продать ВСЮ добычу скупщику (+{inv_data['total_value']:,} монет)".replace(",", " "),
            callback_data="inv_sell_all_full",
        )

    builder.adjust(1)
    return builder.as_markup()


@router.message(F.text.casefold().startswith("инвентарь продать"))
async def handle_inventory_sell_cmd(message: types.Message):
    if not message.from_user:
        return
    text = (message.text or "").strip()
    parts = text.split()
    # Форматы:
    # 1. "инвентарь продать все" / "инвентарь продать всё"
    # 2. "инвентарь продать <предмет>" (по умолчанию 1 шт.)
    # 3. "инвентарь продать <предмет> <кол-во>" (напр. "инвентарь продать заяц 3", "инвентарь продать уголь все")
    if len(parts) < 3:
        await message.reply(
            "💡 <b>Использование команды продажи:</b>\n\n"
            "• <code>инвентарь продать все</code> — продать всю добычу (руда, слитки, рыба, дичь)\n"
            "• <code>инвентарь продать [предмет]</code> — продать 1 шт.\n"
            "• <code>инвентарь продать [предмет] [кол-во|все]</code> — продать нужное количество\n\n"
            "<i>Примеры:</i> <code>инвентарь продать заяц 2</code>, <code>инвентарь продать уголь все</code>, <code>инвентарь продать щука</code>",
            parse_mode="HTML",
        )
        return

    arg_item = parts[2].lower()

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)

        if arg_item in ("все", "всё", "all"):
            try:
                res = await sell_all_inventory(session, user_id=message.from_user.id)
                await message.reply(res.message, parse_mode="HTML")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        cmd = parts[1].lower()
        if cmd in ("инфо", "помощь", "справка", "паспорт"):
            mine = await get_or_create_mine(session, user_id=message.from_user.id)
            upg_cost = (mine.pickaxe_level * 5_000) if mine.pickaxe_level < 10 else "MAX"
            upg_str = f"{upg_cost:,} монет".replace(",", " ") if isinstance(upg_cost, int) else upg_cost
            ores_total = mine.coal + mine.iron + mine.copper + mine.silver + mine.gold

            text = (
                f"⛏ <b>РУДНИК И ШАХТА FABLE: ПОДРОБНАЯ СПРАВКА</b>\n\n"
                f"Шахта — это источник ценных металлов и минералов! Добытая руда накапливается в вашем хранилище, откуда её можно переплавлять в слитки или продавать за монеты.\n\n"
                f"🔨 Уровень вашей кирки: <b>{mine.pickaxe_level} / 10</b>\n"
                f"📦 Всего накоплено руды: <b>{ores_total:,} шт.</b>\n"
                f"🔥 Слитков переплавлено: <b>{mine.processed_metals:,} шт.</b>\n\n"
                f"<b>Цены на скупку руды в инвентаре:</b>\n"
                f"• 🪨 Уголь: <b>{ORE_PRICES['coal']}</b> монет (доступен с 1 ур.)\n"
                f"• 🔩 Железо: <b>{ORE_PRICES['iron']}</b> монет (доступно с 2 ур.)\n"
                f"• 🥉 Медь: <b>{ORE_PRICES['copper']}</b> монет (доступна с 3 ур.)\n"
                f"• 🥈 Серебро: <b>{ORE_PRICES['silver']}</b> монет (доступно с 4 ур.)\n"
                f"• 🥇 Золото: <b>{ORE_PRICES['gold']}</b> монет (доступно с 5 ур.)\n"
                f"• 💎 Металлический слиток (кузница): <b>150</b> монет за слиток\n\n"
                f"🔧 Улучшение кирки до ур. {mine.pickaxe_level + 1}: <b>{upg_str}</b>\n\n"
                f"💡 <b>Команды шахтера:</b>\n"
                f"• <code>шахта</code> — взмахнуть киркой и добыть породу\n"
                f"• <code>шахта улучшить</code> — прокачать кирку (открывает редкие руды)\n"
                f"• <code>шахта переплавка</code> — переплавить металлы в слитки в кузнице\n"
                f"• <code>инвентарь</code> — просмотреть запасы руды и продать их"
            ).replace(",", " ")
            await message.reply(text, parse_mode="HTML")
            return

        # Проверяем, есть ли количество
        quantity = 1
        item_name = arg_item

        # Если предмет состоит из двух слов, например "золотая рыбка"
        if len(parts) >= 4:
            if parts[3].isdigit():
                quantity = int(parts[3])
                item_name = parts[2]
            elif parts[3].lower() in ("все", "всё", "all"):
                quantity = None
                item_name = parts[2]
            elif len(parts) >= 5 and parts[4].isdigit():
                item_name = f"{parts[2]} {parts[3]}"
                quantity = int(parts[4])
            elif len(parts) >= 5 and parts[4].lower() in ("все", "всё", "all"):
                item_name = f"{parts[2]} {parts[3]}"
                quantity = None
            else:
                item_name = " ".join(parts[2:])
                quantity = 1

        try:
            res = await sell_inventory_item(session, user_id=message.from_user.id, item_query=item_name, quantity=quantity)
            await message.reply(res.message, parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().in_(["инвентарь", "!инвентарь"]))
async def handle_inventory(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        inv_data = await get_full_player_inventory(session, user_id=message.from_user.id)

        # 1. Шахта
        mine_lines = []
        if inv_data["mine_items"]:
            for it in inv_data["mine_items"]:
                mine_lines.append(f"• {it.icon} <b>{it.title}</b>: <b>{it.quantity}</b> шт. (по {it.unit_price} монет | итого: <b>{it.total_price:,}</b>)")
        else:
            mine_lines.append("• <i>Руды в хранилище нет. Добудьте её командой: <code>шахта</code></i>")

        # 2. Рыбалка и охота
        loot_lines = []
        if inv_data["farm_loot"]:
            for it in inv_data["farm_loot"]:
                cat_badge = "🐟" if it.category == "fishing" else "🏹"
                loot_lines.append(f"• {cat_badge} {it.icon} <b>{it.title}</b>: <b>{it.quantity}</b> шт. (по {it.unit_price} монет | итого: <b>{it.total_price:,}</b>)")
        else:
            loot_lines.append("• <i>Трофеев нет. Отправьтесь на <code>охота</code> или <code>рыбалка</code>!</i>")

        # 3. Эксклюзивы и предметы магазина
        items_lines = []
        if inv_data["exclusive_items"]:
            for it in inv_data["exclusive_items"]:
                badge = "✨ [Эксклюзив]" if it.is_exclusive else "🏷"
                items_lines.append(f"• {badge} <b>{it.title}</b> — {it.quantity} шт.")
        else:
            items_lines.append("• <i>Эксклюзивов и коллекционных артефактов пока нет</i>")

        total_val = inv_data["total_value"]

        resp = (
            f"🎒 <b>ВАШ ПОЛНЫЙ ИГРОВОЙ ИНВЕНТАРЬ</b>\n\n"
            f"⛏ <b>Полезные ископаемые из шахты:</b>\n"
            + "\n".join(mine_lines)
            + f"\n\n🎯 <b>Добыча с охоты и рыбалки:</b>\n"
            + "\n".join(loot_lines)
            + f"\n\n💎 <b>Коллекции, кейсы и предметы магазина:</b>\n"
            + "\n".join(items_lines)
            + f"\n\n💰 <b>Общая оценочная стоимость всей добычи:</b> <b>{total_val:,}</b> монет\n\n"
            f"📖 <b>Как продавать то, что нафармили:</b>\n"
            f"• Поштучно: <code>инвентарь продать [предмет] [кол-во]</code>\n"
            f"   └ <i>Пример: <code>инвентарь продать заяц 1</code> или <code>инвентарь продать уголь 50</code></i>\n"
            f"• Всё сразу: <code>инвентарь продать все</code>\n"
            f"• Либо нажмите на быструю кнопку продажи ниже 👇".replace(",", " ")
        )
        await message.reply(resp, reply_markup=_build_inventory_keyboard(inv_data), parse_mode="HTML")


@router.callback_query(F.data.startswith("inv_sell_"))
async def on_inventory_sell_click(callback: types.CallbackQuery):
    data_parts = callback.data.split("_")
    # inv_sell_all_full
    # inv_sell_<key>_all
    # inv_sell_<key>_1
    user_id = callback.from_user.id

    async with session_scope() as session:
        if callback.data == "inv_sell_all_full" or (len(data_parts) >= 3 and data_parts[2] == "all" and len(data_parts) == 3):
            try:
                res = await sell_all_inventory(session, user_id=user_id)
                inv_data = await get_full_player_inventory(session, user_id=user_id)
                await callback.answer(f"✅ Продано на +{res.total_earned} монет!", show_alert=True)
                await callback.message.edit_text(
                    res.message,
                    reply_markup=_build_inventory_keyboard(inv_data),
                    parse_mode="HTML",
                )
            except Exception as e:
                try:
                    await callback.answer(f"❌ {e}", show_alert=True)
                except Exception:
                    pass
            return

        item_key = data_parts[2]
        qty = None  # по умолчанию продать все оставшиеся этого типа
        if len(data_parts) >= 4 and data_parts[3].isdigit():
            qty = int(data_parts[3])

        try:
            res = await sell_inventory_item(session, user_id=user_id, item_query=item_key, quantity=qty)
            inv_data = await get_full_player_inventory(session, user_id=user_id)
            try:
                await callback.answer(f"✅ +{res.total_earned} монет!", show_alert=True)
            except Exception:
                pass
            await callback.message.edit_text(
                f"{res.message}\n\n<i>Обновленный инвентарь:</i>",
                reply_markup=_build_inventory_keyboard(inv_data),
                parse_mode="HTML",
            )
        except Exception as e:
            try:
                await callback.answer(f"❌ {e}", show_alert=True)
            except Exception:
                pass


# ===================== РАБОТА И ПОДРАБОТКА =====================

@router.message(F.text.casefold().in_(["работа", "!работа"]))
async def handle_work_cmd(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            reward, exp, msg = await perform_main_job(session, user_id=message.from_user.id)
            await message.reply(msg, parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("подработка"))
async def handle_side_job_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").strip().split()
    if len(parts) == 1:
        lines = [
            "🛠 <b>ДОСТУПНЫЕ ПОДРАБОТКИ</b>\n",
            "Вы можете подработать в свободное время (раз в 10 минут):\n",
        ]
        for code, cfg in SIDE_JOBS.items():
            lines.append(f"{cfg.icon} <b>{cfg.title}</b> — оплата <b>{cfg.min_reward}..{cfg.max_reward}</b> монет | +{cfg.exp_reward} XP\n   └ Команда: <code>подработка {code}</code>")
        await message.reply("\n".join(lines), parse_mode="HTML")
        return

    prof = parts[1].lower()
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            reward, exp, msg = await perform_side_job(session, user_id=message.from_user.id, profession=prof)
            await message.reply(msg, parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


# ===================== ШАХТА =====================

@router.message(F.text.casefold().in_(["шахта", "!шахта"]))
@router.message(F.text.casefold().startswith("шахта"))
async def handle_mine_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").strip().split()
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)

        if len(parts) == 1:
            try:
                res = await dig_in_mine(session, user_id=message.from_user.id)
                await message.reply(res.message, parse_mode="HTML")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        cmd = parts[1].lower()
        if cmd in ("инфо", "помощь", "справка", "паспорт"):
            mine = await get_or_create_mine(session, user_id=message.from_user.id)
            upg_cost = (mine.pickaxe_level * 5_000) if mine.pickaxe_level < 10 else "MAX"
            upg_str = f"{upg_cost:,} монет".replace(",", " ") if isinstance(upg_cost, int) else upg_cost
            ores_total = mine.coal + mine.iron + mine.copper + mine.silver + mine.gold

            text = (
                f"⛏ <b>РУДНИК И ШАХТА FABLE: ПОДРОБНАЯ СПРАВКА</b>\n\n"
                f"Шахта — это источник ценных металлов и минералов! Добытая руда накапливается в вашем хранилище, откуда её можно переплавлять в слитки или продавать за монеты.\n\n"
                f"🔨 Уровень вашей кирки: <b>{mine.pickaxe_level} / 10</b>\n"
                f"📦 Всего накоплено руды: <b>{ores_total:,} шт.</b>\n"
                f"🔥 Слитков переплавлено: <b>{mine.processed_metals:,} шт.</b>\n\n"
                f"<b>Цены на скупку руды в инвентаре:</b>\n"
                f"• 🪨 Уголь: <b>{ORE_PRICES['coal']}</b> монет (доступен с 1 ур.)\n"
                f"• 🔩 Железо: <b>{ORE_PRICES['iron']}</b> монет (доступно с 2 ур.)\n"
                f"• 🥉 Медь: <b>{ORE_PRICES['copper']}</b> монет (доступна с 3 ур.)\n"
                f"• 🥈 Серебро: <b>{ORE_PRICES['silver']}</b> монет (доступно с 4 ур.)\n"
                f"• 🥇 Золото: <b>{ORE_PRICES['gold']}</b> монет (доступно с 5 ур.)\n"
                f"• 💎 Металлический слиток (кузница): <b>150</b> монет за слиток\n\n"
                f"🔧 Улучшение кирки до ур. {mine.pickaxe_level + 1}: <b>{upg_str}</b>\n\n"
                f"💡 <b>Команды шахтера:</b>\n"
                f"• <code>шахта</code> — взмахнуть киркой и добыть породу\n"
                f"• <code>шахта улучшить</code> — прокачать кирку (открывает редкие руды)\n"
                f"• <code>шахта переплавка</code> — переплавить металлы в слитки в кузнице\n"
                f"• <code>инвентарь</code> — просмотреть запасы руды и продать их"
            ).replace(",", " ")
            await message.reply(text, parse_mode="HTML")
            return

        if cmd in ("улучшить", "прокачать", "upgrade"):
            try:
                lvl, cost = await upgrade_pickaxe(session, user_id=message.from_user.id)
                await message.reply(
                    f"⛏ <b>КИРКА УСПЕШНО УЛУЧШЕНА!</b>\n\n"
                    f"Новый уровень: <b>{lvl} / 10</b>\n"
                    f"Потрачено: <b>{cost:,}</b> монет.\n"
                    f"Теперь вам доступны более ценные руды и самоцветы!".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        if cmd in ("переплавка", "плавить", "smelt", "кузница"):
            try:
                bars, msg = await smelt_ores(session, user_id=message.from_user.id)
                await message.reply(msg, parse_mode="HTML")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return


# ===================== РЕЙТИНГ =====================

@router.message(F.text.casefold().in_(["рейтинг", "!рейтинг", "/rating"]))
async def handle_rating_info_cmd(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        p = reg.player
        from app.services.rating_service import BUY_RATING_PRICE, SELL_RATING_PRICE
        text = (
            f"⭐ <b>ИГРОВОЙ РЕЙТИНГ И ВЛИЯНИЕ FABLE</b>\n\n"
            f"Ваш текущий рейтинг: <b>{p.rating:,}</b> очков\n\n"
            f"📊 <b>Курс обмена очков рейтинга:</b>\n"
            f"• Покупка: <b>{BUY_RATING_PRICE}</b> монет за 1 очко\n"
            f"• Продажа: <b>{SELL_RATING_PRICE}</b> монет за 1 очко\n\n"
            f"💡 <b>Команды:</b>\n"
            f"• <code>купить рейтинг [кол-во]</code> — повысить авторитет\n"
            f"• <code>продать рейтинг [кол-во]</code> — обменять очки на монеты"
        ).replace(",", " ")
        await message.reply(text, parse_mode="HTML")


@router.message(F.text.casefold().startswith("купить рейтинг"))
async def handle_buy_rating_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    amount = 1
    for p in reversed(parts):
        if p.isdigit():
            amount = int(p)
            break

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            new_r, cost = await buy_rating(session, user_id=message.from_user.id, amount=amount)
            await message.reply(
                f"⭐ <b>РЕЙТИНГ ПОВЫШЕН!</b>\n\n"
                f"Куплено: <b>+{amount:,}</b> очков рейтинга за <b>{cost:,}</b> монет.\n"
                f"Ваш общий рейтинг: <b>{new_r:,}</b> очков!".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("продать рейтинг"))
async def handle_sell_rating_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    amount = 1
    for p in reversed(parts):
        if p.isdigit():
            amount = int(p)
            break

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            new_r, payout = await sell_rating(session, user_id=message.from_user.id, amount=amount)
            await message.reply(
                f"⭐ <b>РЕЙТИНГ ПРОДАН!</b>\n\n"
                f"Продано: <b>{amount:,}</b> очков рейтинга.\n"
                f"Получено: <b>+{payout:,}</b> монет в кошелек!\n"
                f"Остаток рейтинга: <b>{new_r:,}</b> очков.".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


# ===================== ДОСТИЖЕНИЯ И КВЕСТЫ =====================

@router.message(F.text.casefold().in_([
    "квесты", "!квесты", "/quests", "квест", "!квест", "/quest",
    "достижения", "!достижения", "/achievements", "ачивки", "!ачивки"
]))
async def handle_achievements_cmd(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        from app.services.achievement_service import get_player_achievements, list_achievements
        all_ach = await list_achievements(session)
        user_ach = await get_player_achievements(session, user_id=message.from_user.id)
        unlocked_ids = {a.id for a in user_ach}

        lines = [
            f"🏆 <b>ИГРОВЫЕ ДОСТИЖЕНИЯ И КВЕСТЫ FABLE ({len(unlocked_ids)}/{len(all_ach)})</b>\n",
            "Выполняйте испытания и получайте ценные награды в монетах:\n",
        ]
        for ach in all_ach:
            status_icon = "✅" if ach.id in unlocked_ids else "🔒"
            lines.append(f"{status_icon} <b>{ach.title}</b> — +{ach.reward_coins:,} монет\n   └ <i>{ach.description}</i>".replace(",", " "))

        await message.reply("\n".join(lines), parse_mode="HTML")


# ===================== КЕЙСЫ =====================

@router.message(F.text.casefold().in_(["кейсы", "!кейсы"]))
async def handle_cases_list(message: types.Message):
    lines = [
        "🎁 <b>КЕЙСЫ УДАЧИ FABLE (10 ВИДОВ)</b>\n",
        "Из кейсов выпадают: монеты, рейтинг, майнинг-фермы, бизнесы, недвижимость и эксклюзивы!\n",
    ]
    for c_id, cfg in CASES.items():
        lines.append(f"{cfg.icon} <b>#{c_id} {cfg.name}</b> — <b>{cfg.price:,}</b> монет\n   └ <i>{cfg.description}</i>".replace(",", " "))
    lines.append("\n💡 <i>Чтобы открыть кейс, напишите: <code>кейс открыть [номер]</code> (напр. <code>кейс открыть 1</code>)</i>")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("кейс открыть"))
async def handle_case_open_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    case_id = None
    for p in reversed(parts):
        if p.isdigit():
            case_id = int(p)
            break

    if not case_id or case_id not in CASES:
        await message.reply("Использование: <code>кейс открыть [номер]</code> (от 1 до 10). Список: <code>кейсы</code>", parse_mode="HTML")
        return

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            res = await open_case(session, user_id=message.from_user.id, case_id=case_id)
            await message.reply(res.message, parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


# ===================== МАГАЗИН И НЕДВИЖИМОСТЬ =====================

@router.message(F.text.casefold().in_(["машины", "!машины", "авто", "!авто", "автосалон", "!автосалон"]))
async def handle_cars_list(message: types.Message):
    lines = [
        "🏎 <b>ПРЕМИАЛЬНЫЙ АВТОСАЛОН FABLE (10 МОДЕЛЕЙ)</b>\n",
        "Покупайте автомобили в личный гараж! Машины отражают ваш статус и отображаются в профиле:\n",
    ]
    for c_id, cfg in CARS.items():
        lines.append(
            f"{cfg.icon} <b>#{c_id} {cfg.name}</b> — <b>{cfg.price:,}</b> монет\n"
            f"   ├ Макс. скорость: <b>{cfg.speed_kmh} км/ч</b>\n"
            f"   └ <i>{cfg.description}</i>".replace(",", " ")
        )
    lines.append(
        "\n💡 <i>Купить автомобиль: <code>машина купить [номер]</code> (напр. <code>машина купить 3</code>)\n"
        "Посмотреть свой гараж: <code>гараж</code></i>"
    )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().in_(["гараж", "!гараж", "мои авто", "!мои авто", "мои машины"]))
async def handle_garage(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        cars = await get_player_cars(session, user_id=message.from_user.id)
        if not cars:
            await message.reply("🚗 Ваш гараж пока пуст. Выберите авто в автосалоне: <code>машины</code>", parse_mode="HTML")
            return

        lines = [f"🏎 <b>ЛИЧНЫЙ ГАРАЖ ({len(cars)} авто):</b>\n"]
        for c in cars:
            lines.append(f"• 🚗 <b>{c.item_name}</b>")
        lines.append("\n💡 <i>Купить новые суперкары: <code>машины</code></i>")
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("машина купить"))
@router.message(F.text.casefold().startswith("авто купить"))
@router.message(F.text.casefold().startswith("купить авто"))
@router.message(F.text.casefold().startswith("купить машину"))
async def handle_buy_car_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    car_id = None
    for p in reversed(parts):
        if p.isdigit():
            car_id = int(p)
            break

    if not car_id:
        await message.reply("Использование: <code>машина купить [номер 1..10]</code>. Каталог: <code>машины</code>", parse_mode="HTML")
        return

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            inv, cfg = await buy_car(session, user_id=message.from_user.id, car_id=car_id)
            await message.reply(
                f"🎉 <b>ПОЗДРАВЛЯЕМ С ПОКУПКОЙ АВТОМОБИЛЯ!</b>\n\n"
                f"{cfg.icon} В ваш гараж доставлен: <b>«{cfg.name}»</b>!\n"
                f"Максимальная скорость: <b>{cfg.speed_kmh} км/ч</b>\n"
                f"Цена покупки: <b>{cfg.price:,}</b> монет.\n\n"
                f"Автомобиль добавлен в ваш <code>гараж</code> и гордо украшает <code>профиль</code>!".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().in_(["недвижимость", "!недвижимость"]))
async def handle_properties_list(message: types.Message):
    lines = [
        "🌆 <b>КАТАЛОГ НЕДВИЖИМОСТИ FABLE (10 ВИДОВ)</b>\n",
        "Престижная недвижимость для вложения капитала, статуса и отображения в профиле:\n",
    ]
    for p_id, cfg in PROPERTIES.items():
        lines.append(f"{cfg.icon} <b>#{p_id} {cfg.name}</b> — <b>{cfg.price:,}</b> монет\n   └ <i>{cfg.description}</i>".replace(",", " "))
    lines.append(
        "\n💡 <i>Купить: <code>недвижимость купить [номер]</code> (напр. <code>недвижимость купить 1</code>)\n"
        "Мои владения: <code>моя недвижимость</code></i>"
    )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().in_(["моя недвижимость", "!моя недвижимость"]))
async def handle_my_properties(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        props = await get_player_properties(session, user_id=message.from_user.id)
        if not props:
            await message.reply("🏠 У вас пока нет купленной недвижимости. Каталог: <code>недвижимость</code>", parse_mode="HTML")
            return
        lines = [f"🌆 <b>ВАША НЕДВИЖИМОСТЬ ({len(props)} объектов):</b>\n"]
        for p in props:
            lines.append(f"• 🏠 <b>{p.name}</b>")
        lines.append("\n💡 <i>Купить новые владения: <code>недвижимость</code></i>")
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("недвижимость купить"))
@router.message(F.text.casefold().startswith("купить недвижимость"))
async def handle_buy_property_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    prop_id = None
    for p in reversed(parts):
        if p.isdigit():
            prop_id = int(p)
            break

    if not prop_id:
        await message.reply("Использование: <code>недвижимость купить [номер 1..10]</code>", parse_mode="HTML")
        return

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            prop, cfg = await buy_property(session, user_id=message.from_user.id, property_id=prop_id)
            await message.reply(
                f"🎉 <b>ПОЗДРАВЛЯЕМ С НОВОСЕЛЬЕМ!</b>\n\n"
                f"{cfg.icon} Вы стали владельцем: <b>«{cfg.name}»</b>!\n"
                f"Цена покупки: <b>{cfg.price:,}</b> монет.\n"
                f"Ваша недвижимость теперь гордо украшает ваш <code>профиль</code>!".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().in_(["магазин", "!магазин"]))
async def handle_shop_list(message: types.Message):
    lines = [
        "🛍 <b>ПРЕМИУМ МАГАЗИН FABLE</b>\n",
        "Роскошные товары и суперкары для успешных игроков:\n",
    ]
    for s_id, cfg in SHOP_ITEMS.items():
        lines.append(f"{cfg.icon} <b>#{s_id} {cfg.name}</b> — <b>{cfg.price:,}</b> монет".replace(",", " "))
    lines.append("\n💡 <i>Купить товар: <code>магазин купить [номер]</code></i>")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("магазин купить"))
async def handle_buy_shop_item_cmd(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    item_id = None
    for p in reversed(parts):
        if p.isdigit():
            item_id = int(p)
            break

    if not item_id:
        await message.reply("Использование: <code>магазин купить [номер]</code>", parse_mode="HTML")
        return

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            inv, cfg = await buy_shop_item(session, user_id=message.from_user.id, item_id=item_id)
            await message.reply(
                f"🛍 <b>УСПЕШНАЯ ПОКУПКА В МАГАЗИНЕ!</b>\n\n"
                f"{cfg.icon} Вы приобрели: <b>«{cfg.name}»</b> за <b>{cfg.price:,}</b> монет!\n"
                f"Товар добавлен в ваш <code>инвентарь</code> и виден в профиле.".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


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

        # Уровень админки в текущем чате
        from app.services.moderation_service import get_user_admin_level, ROLE_NAMES
        chat_id = message.chat.id if message.chat else 0
        admin_lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=p.telegram_id, bot=message.bot)
        role_name = ROLE_NAMES.get(admin_lvl, "Игрок")

        # Никнейм
        disp_name = p.custom_nickname or p.first_name

        # Бизнесы
        user_businesses = await get_player_businesses(session, user_id=p.telegram_id)
        if user_businesses:
            b_list_str = ", ".join([f"{BUSINESSES[b.business_id].icon} {BUSINESSES[b.business_id].name} (Ур.{b.level})" for b in user_businesses if b.business_id in BUSINESSES])
        else:
            b_list_str = "Нет предприятий"

        # Фермы
        mining = await get_or_create_mining(session, user_id=p.telegram_id)
        rigs = get_rigs_dict(mining)
        total_rigs = sum(rigs.values())
        active_coin = (mining.active_coin or "BTC").upper()
        hashrate = calculate_effective_hashrate(rigs, active_coin, mining.farm_level)

        # Недвижимость
        from app.services.shop_service import get_player_properties, get_player_cars, get_player_shop_items
        props = await get_player_properties(session, user_id=p.telegram_id)
        props_str = ", ".join([f"🏠 {pr.name}" for pr in props]) if props else "Нет жилья"

        # Автомобили
        cars = await get_player_cars(session, user_id=p.telegram_id)
        cars_str = ", ".join([f"🚗 {c.item_name}" for c in cars]) if cars else "Пешком (нет авто)"

        # Предметы из магазина и кейсов
        items = await get_player_shop_items(session, user_id=p.telegram_id)
        non_car_items = [it for it in items if it.category != "cars"]
        items_str = ", ".join([f"💎 {it.item_name}" for it in non_car_items]) if non_car_items else "Пусто"

        # Брак
        spouse_str = "В активном поиске"
        if p.married_to:
            spouse_player = await session.get(Player, p.married_to)
            spouse_name = spouse_player.custom_nickname or spouse_player.first_name if spouse_player else f"ID {p.married_to}"
            spouse_str = f"💍 В браке с <b>{spouse_name}</b>"

        text = (
            f"👤 <b>Профиль игрока {disp_name}</b>\n\n"
            f"🆔 ID: <code>{p.telegram_id}</code>\n"
            f"🛡 Уровень админки: <b>{admin_lvl} ({role_name})</b>\n"
            f"✨ Опыт: <b>{p.experience:,}</b> XP\n"
            f"💰 Кошелек: <b>{p.wallet:,}</b> монет\n"
            f"🏦 Банк: <b>{p.bank_balance:,}</b> монет\n"
            f"📈 Биржевые акции: <b>{p.shares}</b> ед.\n"
            f"⭐ Рейтинг: <b>{p.rating:,}</b> очков\n"
            f"🤝 Репутация: <b>{p.reputation}</b>\n"
            f"💍 Семья: {spouse_str}\n\n"
            f"🏢 <b>Бизнесы:</b> {b_list_str}\n"
            f"⛏ <b>Майнинг-ферма:</b> {total_rigs} ригов ({hashrate:,} GH/s, активна: {active_coin})\n"
            f"🏎 <b>Гараж / Авто:</b> {cars_str}\n"
            f"🌆 <b>Недвижимость:</b> {props_str}\n"
            f"🛍 <b>Предметы / Магазин:</b> {items_str}".replace(",", " ")
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
        if len(parts) == 1 or parts[1].casefold() in ("список", "лист", "инфо", "помощь"):
            user_businesses = await get_player_businesses(session, user_id=message.from_user.id)
            user_b_map = {b.business_id: b for b in user_businesses}

            # Расчёт накопленной кассы
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            total_uncollected = 0
            total_hourly = 0
            for ub in user_businesses:
                inc_hour = get_hourly_income(ub.business_id, ub.level)
                total_hourly += inc_hour
                last = ub.last_collected_at or now
                elapsed_hours = (now - last).total_seconds() / 3600.0
                capped_hours = min(elapsed_hours, 4.0)
                if capped_hours >= (1.0 / 60.0):
                    total_uncollected += int(inc_hour * capped_hours)

            from app.db.models import Player
            curr_p = await session.get(Player, message.from_user.id)
            p_wallet = curr_p.wallet if curr_p else 0

            lines = [
                "🏢 <b>КОММЕРЧЕСКИЕ БИЗНЕСЫ FABLE</b>\n",
                "Бизнесы — это ваш источник пассивного дохода! Вы покупаете предприятия, улучшаете их уровень и собираете прибыль в кошелёк.",
                f"💵 Наличные в кошельке: <b>{p_wallet:,}</b> монет",
                f"📈 Ваш совокупный доход от предприятий: <b>+{total_hourly:,}</b> монет/час",
                f"📥 В кассе предприятий готово к сбору: <b>+{total_uncollected:,}</b> монет\n",
                "⚠️ <b>ВАЖНО:</b> Касса копится максимум <b>4 часа</b>! Если не собирать, бизнес прекращает работу до следующего сбора.\n",
                "<b>📋 Каталог предприятий (10 уровней развития):</b>\n",
            ]
            for b_id, cfg in BUSINESSES.items():
                if b_id in user_b_map:
                    ub = user_b_map[b_id]
                    inc = get_hourly_income(b_id, ub.level)
                    upg = get_upgrade_cost(b_id, ub.level) if ub.level < 10 else "MAX"
                    upg_str = f"{upg:,} монет".replace(",", " ") if isinstance(upg, int) else upg

                    # Накоплено в кассе конкретного бизнеса
                    last = ub.last_collected_at or now
                    b_hours = min((now - last).total_seconds() / 3600.0, 4.0)
                    b_cash = int(inc * b_hours) if b_hours >= (1.0 / 60.0) else 0
                    daily = inc * 24
                    payback = (cfg.base_cost / daily) if daily > 0 else 0

                    lines.append(
                        f"{cfg.icon} <b>#{b_id} {cfg.name}</b> [Владеете: Ур. {ub.level}/10]\n"
                        f"   ├ Доход: <b>+{inc:,}/час</b> (макс за 4ч: +{inc * 4:,} | в сутки: +{daily:,})\n"
                        f"   ├ В кассе сейчас: <b>{b_cash:,}</b> монет | Окупаемость: ~{payback:.1f} дн.\n"
                        f"   └ Прокачка ур. {ub.level + 1}: <b>{upg_str}</b>".replace(",", " ")
                    )
                else:
                    daily = cfg.base_income_hourly * 24
                    payback = (cfg.base_cost / daily) if daily > 0 else 0
                    lines.append(
                        f"{cfg.icon} <b>#{b_id} {cfg.name}</b> (Не приобретен)\n"
                        f"   ├ Стоимость покупки: <b>{cfg.base_cost:,}</b> монет\n"
                        f"   ├ Базовый доход: <b>+{cfg.base_income_hourly:,}/час</b> (в сутки: +{daily:,})\n"
                        f"   ├ Окупаемость вложений: ~{payback:.1f} дн.\n"
                        f"   └ Купить: <code>бизнес купить {b_id}</code>".replace(",", " ")
                    )

            lines.append(
                "\n💡 <b>Команды управления бизнесом:</b>\n"
                "• <code>бизнес купить [номер 1..10]</code> — приобрести новое предприятие\n"
                "• <code>бизнес улучшить [номер]</code> — прокачать уровень (увеличивает доход на +60%)\n"
                "• <code>бизнес собрать</code> — забрать накопленную прибыль со всех предприятий в кошелек"
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


@router.message(F.text.casefold().in_(["купить ферму", "!купить ферму"]))
async def handle_buy_farm_shortcut(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            m, cost = await buy_computers(session, user_id=message.from_user.id, count=1, rig_type="cpu")
            cfg = MINING_RIGS["cpu"]
            await message.reply(
                f"🎉 <b>Майнинг-ферма успешно приобретена!</b>\n\n"
                f"Установлен базовый компьютер: {cfg.icon} <b>{cfg.name}</b> за <b>{cost:,}</b> монет.\n"
                f"Мощность рига: <b>{cfg.hashrate} GH/s</b>\n"
                f"Добываемые монеты: <b>{', '.join(cfg.allowed_coins)}</b>\n\n"
                f"💡 Напишите <code>ферма</code> для просмотра панели фермы или <code>ферма монета TON</code> для выбора добычи!".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


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
        if len(parts) == 1 or parts[0].casefold() in ("майнинг", "!майнинг") or parts[1].casefold() in ("инфо", "помощь"):
            rigs = get_rigs_dict(mining)
            total_comps = sum(rigs.values())
            active_coin = (mining.active_coin or "BTC").upper()
            hashrate = calculate_effective_hashrate(rigs, active_coin, mining.farm_level)
            upg_cost = get_farm_upgrade_cost(mining.farm_level) if mining.farm_level < 10 else "MAX"
            upg_str = f"{upg_cost:,} монет".replace(",", " ") if isinstance(upg_cost, int) else upg_cost

            rigs_lines = []
            for r_code, cfg in MINING_RIGS.items():
                count = rigs.get(r_code, 0)
                rigs_lines.append(
                    f"{cfg.icon} <b>{cfg.name}</b> (код: <code>{r_code}</code>)\n"
                    f"   ├ В наличии: <b>{count} шт.</b> | Стоимость: <b>{cfg.price:,}</b> монет\n"
                    f"   ├ Хэшрейт одного рига: <b>+{cfg.hashrate} GH/s</b>\n"
                    f"   └ Специализация монет: <b>{', '.join(cfg.allowed_coins)}</b>".replace(",", " ")
                )

            # Расчет детальной часовой, 4-часовой и суточной добычи
            from app.services.mining_service import COIN_BASE_YIELD_PER_HASHRATE
            yield_rate = COIN_BASE_YIELD_PER_HASHRATE.get(active_coin, Decimal("0.00000005"))
            hourly_crypto = (Decimal(str(hashrate)) * yield_rate).quantize(Decimal("0.00000001"))
            cap_crypto = (hourly_crypto * Decimal("4.0")).quantize(Decimal("0.00000001"))
            daily_crypto = (hourly_crypto * Decimal("24.0")).quantize(Decimal("0.00000001"))
            hourly_crypto_str = f"{hourly_crypto:.8f}".rstrip("0").rstrip(".")
            cap_crypto_str = f"{cap_crypto:.8f}".rstrip("0").rstrip(".")
            daily_crypto_str = f"{daily_crypto:.8f}".rstrip("0").rstrip(".")

            resp = (
                f"⛏ <b>КРИПТО-МАЙНИНГ ДАТА-ЦЕНТР FABLE</b>\n\n"
                f"Майнинг позволяет добывать реальные крипто-активы прямо на ваш биржевой <code>портфель</code>, а также приносит вспомогательные игровые монеты!\n\n"
                f"🎯 Добываемая сейчас криптовалюта: <b>{active_coin}</b>\n"
                f"🏢 Уровень дата-центра: <b>{mining.farm_level} / 10</b> (+{(mining.farm_level - 1)*25}% бонус мощности)\n"
                f"🔥 Суммарная мощность майнинга: <b>{hashrate:,} GH/s</b>\n"
                f"🖥 Всего ригов на ферме: <b>{total_comps} шт.</b>\n\n"
                f"📊 <b>Прогноз добычи выбранной монеты ({active_coin}):</b>\n"
                f"   ├ Доход в час: <b>+{hourly_crypto_str or '0'} {active_coin}</b> (+{int(hashrate*0.5):,} монет)\n"
                f"   ├ Накопитель кассы (за 4ч капа): <b>до +{cap_crypto_str or '0'} {active_coin}</b> (+{int(hashrate*2):,} монет)\n"
                f"   └ В сутки (при сборе): <b>~+{daily_crypto_str or '0'} {active_coin}</b>\n\n"
                f"⚠️ <b>Внимание:</b> Накопитель майнинга заполняется за <b>4 часа</b>, после чего добыча останавливается до нажатия <code>ферма собрать</code>!\n\n"
                f"<b>Каталог оборудования для фермы:</b>\n"
                + "\n".join(rigs_lines)
                + f"\n\n🔧 Прокачка дата-центра до ур. {mining.farm_level + 1}: <b>{upg_str}</b>\n\n"
                f"💡 <b>Команды управления майнингом:</b>\n"
                f"• <code>купить ферму</code> — приобрести первый CPU-компьютер\n"
                f"• <code>ферма купить [код_рига] [кол-во]</code> — купить риги (напр. <code>ферма купить gpu 2</code>)\n"
                f"• <code>ферма монета [BTC|ETH|TON|SOL|DOGE]</code> — сменить добываемую монету\n"
                f"• <code>ферма собрать</code> — отправить намайненную крипту в <code>портфель</code>\n"
                f"• <code>ферма улучшить</code> — прокачать уровень дата-центра (+25% к мощности)"
            ).replace(",", " ")
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


# ===================== КОСМОДРОМ И РАКЕТЫ =====================

@router.message(F.text.casefold().in_(["космодром", "!космодром"]))
@router.message(F.text.casefold().startswith("космодром"))
async def handle_cosmodrome(message: types.Message):
    if not message.from_user:
        return
    text = (message.text or "").strip()
    parts = text.split()

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        cosmo = await get_or_create_cosmodrome(session, user_id=message.from_user.id)
        rockets = get_rockets_dict(cosmo)

        if len(parts) == 1 or parts[1].lower() in ("инфо", "помощь"):
            total_rockets = sum(rockets.values())
            hourly_inc = calculate_hourly_income(rockets, cosmo.level)
            cap_inc = int(hourly_inc * 4.0)
            daily_inc = hourly_inc * 24

            # Накоплено прямо сейчас
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            last = cosmo.last_collected_at or now
            hours = min((now - last).total_seconds() / 3600.0, 4.0)
            cur_cash = int(hourly_inc * hours) if hours >= (1.0 / 60.0) else 0

            upg_cost = get_cosmodrome_upgrade_cost(cosmo.level) if cosmo.level < 10 else "MAX"
            upg_str = f"{upg_cost:,} монет".replace(",", " ") if isinstance(upg_cost, int) else upg_cost

            rocket_lines = []
            for r_code, cfg in ROCKETS.items():
                cnt = rockets.get(r_code, 0)
                status = f"✅ <b>В ангаре: {cnt} шт.</b>" if cnt > 0 else f"Стоимость: <b>{cfg.price:,}</b> монет"
                daily = cfg.hourly_income * 24
                payback = (cfg.price / daily) if daily > 0 else 0
                rocket_lines.append(
                    f"{cfg.icon} <b>{cfg.name}</b> (код: <code>{r_code}</code>)\n"
                    f"   ├ Статус: {status} | Требует космодром: ур. {cfg.min_cosmodrome_level}\n"
                    f"   ├ Прибыль: <b>+{cfg.hourly_income:,}/час</b> (+{daily:,}/сутки) | Окупаемость: ~{payback:.1f} дн.\n"
                    f"   └ <i>{cfg.description}</i>".replace(",", " ")
                )

            resp = (
                f"🌌 <b>ОРБИТАЛЬНЫЙ КОСМОДРОМ FABLE</b>\n\n"
                f"Космодром — это аэрокосмический комплекс! Ракеты генерируют пассивный доход от орбитальных контрактов, а также запускаются в прибыльные исследовательские экспедиции.\n\n"
                f"🚀 Уровень комплекса: <b>{cosmo.level} / 10</b> (Бонус к прибыли: +{(cosmo.level - 1)*20}%)\n"
                f"🛸 Всего ракет в ангаре: <b>{total_rockets}</b> шт. | Успешных экспедиций: <b>{cosmo.total_launches}</b>\n"
                f"📈 <b>Финансовые показатели космодрома:</b>\n"
                f"   ├ Доход от контрактов: <b>+{hourly_inc:,}</b> монет/час (в сутки: +{daily_inc:,})\n"
                f"   ├ Накопитель кассы (за 4ч капа): <b>до +{cap_inc:,}</b> монет\n"
                f"   └ Готово к сбору прямо сейчас: <b>+{cur_cash:,}</b> монет\n\n"
                f"⚠️ <b>Внимание:</b> Прибыль ракет копится максимум <b>4 часа</b>! Не забывайте писать <code>космодром собрать</code>.\n\n"
                f"<b>Каталог доступных космических кораблей:</b>\n"
                + "\n".join(rocket_lines)
                + f"\n\n🔧 Модернизация стартовой площадки до ур. {cosmo.level + 1}: <b>{upg_str}</b>\n\n"
                f"💡 <b>Команды управления космодромом:</b>\n"
                f"• <code>космодром купить [код_ракеты] [кол-во]</code> — купить ракету (напр. <code>космодром купить vostok 1</code>)\n"
                f"• <code>космодром собрать</code> — перевести прибыль коммерческих рейсов в кошелек\n"
                f"• <code>космодром запуск</code> — отправить ракету в космическую экспедицию за разовой наградой\n"
                f"• <code>космодром улучшить</code> — прокачать стартовую площадку (+20% к доходу всех ракет)"
            )
            await message.reply(resp, parse_mode="HTML")
            return

        cmd = parts[1].lower()
        if cmd in ("собрать", "прибыль", "доход", "collect"):
            try:
                profit, count = await collect_cosmodrome_profits(session, user_id=message.from_user.id)
                if profit > 0:
                    await message.reply(
                        f"🌌 <b>Прибыль с космодрома собрана!</b>\n\n"
                        f"Ваши <b>{count}</b> ракет успешно завершили рейсы!\n"
                        f"Зачислено: <b>+{profit:,}</b> монет!".replace(",", " "),
                        parse_mode="HTML",
                    )
                else:
                    await message.reply("⏳ Ракеты еще в пути или ангар пуст. Сбор возможен при накоплении от 1 минуты!")
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        if cmd in ("запуск", "миссия", "launch"):
            r_code = parts[2].lower() if len(parts) >= 3 else None
            try:
                res = await launch_rocket_mission(session, user_id=message.from_user.id, rocket_code=r_code)
                await message.reply(
                    f"🚀 <b>УСПЕШНЫЙ ЗАПУСК РАКЕТЫ {res.rocket_name.upper()}!</b>\n\n"
                    f"{res.story}\n\n"
                    f"💰 Награда за полет: <b>+{res.reward_coins:,}</b> монет\n"
                    f"✨ Космический опыт: <b>+{res.reward_exp}</b> XP".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        if cmd in ("купить", "buy") and len(parts) >= 3:
            r_code = parts[2].lower()
            cnt = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 1
            try:
                c, cfg, cost = await buy_rocket(session, user_id=message.from_user.id, rocket_code=r_code, count=cnt)
                await message.reply(
                    f"🚀 <b>РАКЕТА ПРИОБРЕТЕНА!</b>\n\n"
                    f"Ангар пополнился: <b>{cfg.name}</b> {cfg.icon} ({cnt} шт.)!\n"
                    f"Сумма контракта: <b>{cost:,}</b> монет.\n"
                    f"Она приносит <b>+{cfg.hourly_income * cnt:,}</b> монет/час!\n"
                    f"Отправьте ее на орбиту: <code>космодром запуск</code>!".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        if cmd in ("улучшить", "upgrade", "прокачать"):
            try:
                c, cost = await upgrade_cosmodrome(session, user_id=message.from_user.id)
                await message.reply(
                    f"🌌 <b>КОСМОДРОМ УСПЕШНО УЛУЧШЕН!</b>\n\n"
                    f"Новый уровень: <b>{c.level} / 10</b>!\n"
                    f"Потрачено: <b>{cost:,}</b> монет.\n"
                    f"Бонус к доходности всех полетов: <b>+{(c.level - 1)*20}%</b>!".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return


# ===================== СНАРЯЖЕНИЕ И УЛУЧШЕНИЯ =====================

@router.message(F.text.casefold().in_(["снаряжение", "!снаряжение", "экипировка", "!экипировка"]))
async def handle_gear_panel(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        gear = await get_or_create_gear(session, user_id=message.from_user.id)
        mine = await get_or_create_mine(session, user_id=message.from_user.id)

        lines = [
            "🛡 <b>АРСЕНАЛ И СНАРЯЖЕНИЕ FABLE</b>\n",
            "Улучшение экипировки увеличивает прибыль подработок, шахты и охоты:\n",
            f"🔫 <b>Охотничье ружьё</b>: Ур. <b>{gear.hunting_rifle_level} / 10</b> (Увеличивает меткость и шанс редких зверей)",
            f"🎣 <b>Рыболовная удочка</b>: Ур. <b>{gear.fishing_rod_level} / 10</b> (Снижает сход рыбы и даёт сундуки)",
            f"⛏ <b>Шахтерская кирка</b>: Ур. <b>{mine.pickaxe_level} / 10</b> (Открывает железо, медь, серебро, золото)",
            f"🚕 <b>Рок-машина такси</b>: Ур. <b>{gear.taxi_car_level} / 10</b> (+{get_gear_bonus_percent(gear.taxi_car_level)}% к оплате заказов)",
            f"🛵 <b>Транспорт курьера</b>: Ур. <b>{gear.courier_bike_level} / 10</b> (+{get_gear_bonus_percent(gear.courier_bike_level)}% к оплате)",
            f"☕️ <b>Кофемашина бариста</b>: Ур. <b>{gear.barista_machine_level} / 10</b> (+{get_gear_bonus_percent(gear.barista_machine_level)}% к чаевым)",
            f"🦾 <b>Экзоскелет грузчика</b>: Ур. <b>{gear.loader_exosuit_level} / 10</b> (+{get_gear_bonus_percent(gear.loader_exosuit_level)}% к ставке)",
            f"🧼 <b>Инвентарь клинера</b>: Ур. <b>{gear.cleaner_gear_level} / 10</b> (+{get_gear_bonus_percent(gear.cleaner_gear_level)}% к чеку)",
            "\n💡 <i>Чтобы улучшить предмет, напишите:\n"
            "• <code>улучшить оружие</code>\n"
            "• <code>улучшить удочка</code>\n"
            "• <code>улучшить кирка</code>\n"
            "• <code>улучшить такси</code>\n"
            "• <code>улучшить курьер</code>\n"
            "• <code>улучшить бариста</code>\n"
            "• <code>улучшить грузчик</code>\n"
            "• <code>улучшить уборщик</code></i>",
        ]
        await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("улучшить"))
async def handle_gear_upgrade(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.reply("Использование: <code>улучшить [оружие|удочка|кирка|такси|курьер|бариста|грузчик|уборщик]</code>", parse_mode="HTML")
        return

    item_name = parts[1].lower()
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)

        if item_name in ("кирка", "кирку", "шахта"):
            try:
                lvl, cost = await upgrade_pickaxe(session, user_id=message.from_user.id)
                await message.reply(
                    f"⛏ <b>КИРКА УСПЕШНО УЛУЧШЕНА!</b>\n\n"
                    f"Новый уровень: <b>{lvl} / 10</b>\n"
                    f"Потрачено: <b>{cost:,}</b> монет.\n"
                    f"Теперь вам доступны более ценные металлы в забое!".replace(",", " "),
                    parse_mode="HTML",
                )
            except Exception as e:
                await message.reply(f"❌ {e}")
            return

        try:
            name, new_lvl, cost = await upgrade_player_gear(session, user_id=message.from_user.id, gear_type=item_name)
            await message.reply(
                f"⚡️ <b>СНАРЯЖЕНИЕ УСПЕШНО УЛУЧШЕНО!</b>\n\n"
                f"Предмет: <b>{name}</b>\n"
                f"Новый уровень: <b>{new_lvl} / 10</b>\n"
                f"Потрачено: <b>{cost:,}</b> монет.\n"
                f"Ваша прибыль и бонусы от активности возросли!".replace(",", " "),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


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
