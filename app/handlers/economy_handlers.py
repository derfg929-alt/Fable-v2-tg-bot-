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
from app.services.mine_service import (
    get_or_create_mine,
    dig_in_mine,
    upgrade_pickaxe,
    smelt_ores,
    ORE_PRICES,
)
from app.services.job_service import perform_main_job, perform_side_job, SIDE_JOBS
from app.services.rating_service import buy_rating, sell_rating
from app.services.case_service import open_case, CASES
from app.services.shop_service import (
    buy_property,
    buy_shop_item,
    get_player_properties,
    get_player_shop_items,
    PROPERTIES,
    SHOP_ITEMS,
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


def _build_inventory_keyboard(mine):
    builder = InlineKeyboardBuilder()
    if mine.coal > 0:
        builder.button(text=f"🪨 Продать уголь ({mine.coal} шт.)", callback_data="inv_sell_coal")
    if mine.iron > 0:
        builder.button(text=f"🔩 Продать железо ({mine.iron} шт.)", callback_data="inv_sell_iron")
    if mine.copper > 0:
        builder.button(text=f"🥉 Продать медь ({mine.copper} шт.)", callback_data="inv_sell_copper")
    if mine.silver > 0:
        builder.button(text=f"🥈 Продать серебро ({mine.silver} шт.)", callback_data="inv_sell_silver")
    if mine.gold > 0:
        builder.button(text=f"🥇 Продать золото ({mine.gold} шт.)", callback_data="inv_sell_gold")
    if mine.processed_metals > 0:
        builder.button(text=f"💎 Продать слитки ({mine.processed_metals} шт.)", callback_data="inv_sell_metals")

    total_ores = mine.coal + mine.iron + mine.copper + mine.silver + mine.gold + mine.processed_metals
    if total_ores > 0:
        builder.button(text="💰 Продать ВСЮ руду и слитки", callback_data="inv_sell_all")

    builder.adjust(1)
    return builder.as_markup()


@router.message(F.text.casefold().in_(["инвентарь", "!инвентарь"]))
async def handle_inventory(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        mine = await get_or_create_mine(session, user_id=message.from_user.id)
        items = await get_player_shop_items(session, user_id=message.from_user.id)

        items_lines = []
        for it in items:
            badge = "✨ [Эксклюзив]" if it.is_exclusive else "🏷"
            items_lines.append(f"• {badge} <b>{it.item_name}</b> — {it.quantity} шт.")
        items_str = "\n".join(items_lines) if items_lines else "• Нет предметов из магазина или кейсов"

        total_value = (
            mine.coal * ORE_PRICES["coal"]
            + mine.iron * ORE_PRICES["iron"]
            + mine.copper * ORE_PRICES["copper"]
            + mine.silver * ORE_PRICES["silver"]
            + mine.gold * ORE_PRICES["gold"]
            + mine.processed_metals * ORE_PRICES["processed_metals"]
        )

        resp = (
            f"🎒 <b>ВАШ ИГРОВОЙ ИНВЕНТАРЬ</b>\n\n"
            f"⛏ <b>Добытые полезные ископаемые:</b>\n"
            f"• 🪨 Уголь: <b>{mine.coal}</b> шт. (по {ORE_PRICES['coal']} монет)\n"
            f"• 🔩 Железо: <b>{mine.iron}</b> шт. (по {ORE_PRICES['iron']} монет)\n"
            f"• 🥉 Медь: <b>{mine.copper}</b> шт. (по {ORE_PRICES['copper']} монет)\n"
            f"• 🥈 Серебро: <b>{mine.silver}</b> шт. (по {ORE_PRICES['silver']} монет)\n"
            f"• 🥇 Золото: <b>{mine.gold}</b> шт. (по {ORE_PRICES['gold']} монет)\n"
            f"• 💎 Металлические слитки: <b>{mine.processed_metals}</b> шт. (по {ORE_PRICES['processed_metals']} монет)\n"
            f"💵 Общая оценочная стоимость ресурсов: <b>{total_value:,}</b> монет\n\n"
            f"🛍 <b>Предметы и артефакты:</b>\n{items_str}\n\n"
            f"<i>Нажмите кнопку ниже, чтобы продать накопленную руду:</i>".replace(",", " ")
        )
        await message.reply(resp, reply_markup=_build_inventory_keyboard(mine), parse_mode="HTML")


@router.callback_query(F.data.startswith("inv_sell_"))
async def on_inventory_sell_click(callback: types.CallbackQuery):
    action = callback.data.split("_")[2]
    user_id = callback.from_user.id

    async with session_scope() as session:
        mine = await get_or_create_mine(session, user_id=user_id)
        earned = 0
        desc_text = ""

        if action == "coal" and mine.coal > 0:
            earned = mine.coal * ORE_PRICES["coal"]
            desc_text = f"🪨 Продано {mine.coal} шт. угля за <b>+{earned:,}</b> монет!"
            mine.coal = 0
        elif action == "iron" and mine.iron > 0:
            earned = mine.iron * ORE_PRICES["iron"]
            desc_text = f"🔩 Продано {mine.iron} шт. железа за <b>+{earned:,}</b> монет!"
            mine.iron = 0
        elif action == "copper" and mine.copper > 0:
            earned = mine.copper * ORE_PRICES["copper"]
            desc_text = f"🥉 Продано {mine.copper} шт. меди за <b>+{earned:,}</b> монет!"
            mine.copper = 0
        elif action == "silver" and mine.silver > 0:
            earned = mine.silver * ORE_PRICES["silver"]
            desc_text = f"🥈 Продано {mine.silver} шт. серебра за <b>+{earned:,}</b> монет!"
            mine.silver = 0
        elif action == "gold" and mine.gold > 0:
            earned = mine.gold * ORE_PRICES["gold"]
            desc_text = f"🥇 Продано {mine.gold} шт. золота за <b>+{earned:,}</b> монет!"
            mine.gold = 0
        elif action == "metals" and mine.processed_metals > 0:
            earned = mine.processed_metals * ORE_PRICES["processed_metals"]
            desc_text = f"💎 Продано {mine.processed_metals} слитков за <b>+{earned:,}</b> монет!"
            mine.processed_metals = 0
        elif action == "all":
            total = (
                mine.coal * ORE_PRICES["coal"]
                + mine.iron * ORE_PRICES["iron"]
                + mine.copper * ORE_PRICES["copper"]
                + mine.silver * ORE_PRICES["silver"]
                + mine.gold * ORE_PRICES["gold"]
                + mine.processed_metals * ORE_PRICES["processed_metals"]
            )
            if total > 0:
                earned = total
                desc_text = f"💰 Вся руда и слитки успешно проданы скупщику за <b>+{earned:,}</b> монет!"
                mine.coal = mine.iron = mine.copper = mine.silver = mine.gold = mine.processed_metals = 0

        if earned > 0:
            await credit_coins(session, user_id=user_id, amount=earned, reason="Продажа руды из шахты", operation_type="mine_sell_ore")
            await callback.answer(f"✅ Продано на +{earned} монет!", show_alert=True)
            await callback.message.edit_text(
                f"✅ <b>СДЕЛКА ЗАВЕРШЕНА!</b>\n\n{desc_text}".replace(",", " "),
                reply_markup=_build_inventory_keyboard(mine),
                parse_mode="HTML",
            )
        else:
            await callback.answer("У вас нет этого ресурса для продажи!", show_alert=True)


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

@router.message(F.text.casefold().in_(["недвижимость", "!недвижимость"]))
async def handle_properties_list(message: types.Message):
    lines = [
        "🌆 <b>КАТАЛОГ НЕДВИЖИМОСТИ FABLE (10 ВИДОВ)</b>\n",
        "Престижная недвижимость для вложения капитала и статуса в профиле:\n",
    ]
    for p_id, cfg in PROPERTIES.items():
        lines.append(f"{cfg.icon} <b>#{p_id} {cfg.name}</b> — <b>{cfg.price:,}</b> монет\n   └ <i>{cfg.description}</i>".replace(",", " "))
    lines.append("\n💡 <i>Купить: <code>недвижимость купить [номер]</code> (напр. <code>недвижимость купить 1</code>)</i>")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().startswith("недвижимость купить"))
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
        req_exp = p.level * 100
        to_next = max(0, req_exp - p.experience)

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
        from app.services.shop_service import get_player_properties, get_player_shop_items
        props = await get_player_properties(session, user_id=p.telegram_id)
        props_str = ", ".join([f"🏠 {pr.name}" for pr in props]) if props else "Нет жилья"

        # Предметы из магазина и кейсов
        items = await get_player_shop_items(session, user_id=p.telegram_id)
        items_str = ", ".join([f"💎 {it.item_name}" for it in items]) if items else "Пусто"

        # Брак
        spouse_str = "В активном поиске"
        if p.married_to:
            spouse_player = await session.get(Player, p.married_to)
            spouse_name = spouse_player.custom_nickname or spouse_player.first_name if spouse_player else f"ID {p.married_to}"
            spouse_str = f"💍 В браке с <b>{spouse_name}</b>"

        text = (
            f"👤 <b>Профиль игрока {disp_name}</b>\n\n"
            f"🆔 ID: <code>{p.telegram_id}</code>\n"
            f"⭐ Уровень: <b>{p.level}</b>\n"
            f"✨ Опыт: <b>{p.experience} / {req_exp}</b> (до след.: {to_next})\n"
            f"💰 Кошелек: <b>{p.wallet:,}</b> монет\n"
            f"🏦 Банк: <b>{p.bank_balance:,}</b> монет\n"
            f"📈 Биржевые акции: <b>{p.shares}</b> ед.\n"
            f"⭐ Рейтинг: <b>{p.rating:,}</b> очков\n"
            f"🤝 Репутация: <b>{p.reputation}</b>\n"
            f"💍 Семья: {spouse_str}\n\n"
            f"🏢 <b>Бизнесы:</b> {b_list_str}\n"
            f"⛏ <b>Майнинг-ферма:</b> {total_rigs} ригов ({hashrate:,} GH/s, активна: {active_coin})\n"
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
        if len(parts) == 1 or parts[1].casefold() in ("список", "лист"):
            user_businesses = await get_player_businesses(session, user_id=message.from_user.id)
            user_b_map = {b.business_id: b for b in user_businesses}

            # Расчёт накопленной кассы
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            total_uncollected = 0
            for ub in user_businesses:
                last = ub.last_collected_at or now
                elapsed_hours = (now - last).total_seconds() / 3600.0
                capped_hours = min(elapsed_hours, 4.0)
                if capped_hours >= (1.0 / 60.0):
                    inc_hour = get_hourly_income(ub.business_id, ub.level)
                    total_uncollected += int(inc_hour * capped_hours)

            from app.db.models import Player
            curr_p = await session.get(Player, message.from_user.id)
            p_wallet = curr_p.wallet if curr_p else 0

            lines = [
                "🏢 <b>КОММЕРЧЕСКИЕ БИЗНЕСЫ FABLE</b>\n",
                f"💰 Ваш баланс кошелька: <b>{p_wallet:,}</b> монет".replace(",", " "),
                f"📥 В кассе предприятий готово к сбору: <b>+{total_uncollected:,}</b> монет\n".replace(",", " "),
                "⏱ <i>Прибыль копится максимум 4 часа! Если не собирать, предприятия прекращают генерацию прибыли.</i>\n",
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

                    lines.append(
                        f"{cfg.icon} <b>#{b_id} {cfg.name}</b> [Ур. {ub.level}/10]\n"
                        f"   ├ Доход: <b>+{inc:,}/ч</b> (макс. за 4ч: +{inc * 4:,})\n"
                        f"   ├ В кассе бизнеса: <b>{b_cash:,}</b> монет\n"
                        f"   └ Улучшение: {upg_str}".replace(",", " ")
                    )
                else:
                    lines.append(f"{cfg.icon} <b>#{b_id} {cfg.name}</b> (Не куплен)\n   ├ Доход: +{cfg.base_income_hourly:,}/ч\n   └ Стоимость: <b>{cfg.base_cost:,}</b> монет".replace(",", " "))

            lines.append(
                "\n<i>Команды:</i>\n"
                "• <code>бизнес купить [номер]</code> — купить предприятие\n"
                "• <code>бизнес улучшить [номер]</code> — прокачать уровень\n"
                "• <code>бизнес собрать</code> — забрать кассу со всех предприятий"
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
                rigs_lines.append(f"{cfg.icon} <b>{cfg.name}</b>: <b>{count}</b> шт. (Мощность: {cfg.hashrate} GH/s | Добывает: {', '.join(cfg.allowed_coins)} | Цена: {cfg.price:,} монет)".replace(",", " "))

            # Расчет примерной часовой добычи для активной монеты
            from app.services.mining_service import COIN_BASE_YIELD_PER_HASHRATE
            yield_rate = COIN_BASE_YIELD_PER_HASHRATE.get(active_coin, Decimal("0.00000005"))
            hourly_crypto = (Decimal(str(hashrate)) * yield_rate).quantize(Decimal("0.00000001"))
            hourly_crypto_str = f"{hourly_crypto:.8f}".rstrip("0").rstrip(".")

            resp = (
                f"⛏ <b>КРИПТО-МАЙНИНГ ФЕРМА FABLE</b>\n\n"
                f"🎯 Активная криптовалюта: <b>{active_coin}</b>\n"
                f"⚡️ Уровень фермы: <b>{mining.farm_level} / 10</b> (Бонус к мощности: +{(mining.farm_level - 1)*25}%)\n"
                f"🔥 Общая вычислительная мощность: <b>{hashrate:,} GH/s</b>\n"
                f"⛏ Добыча активной монеты: <b>~{hourly_crypto_str} {active_coin}/час</b>\n"
                f"🖥 Всего оборудования на ферме: <b>{total_comps}</b> шт.\n\n"
                f"<b>Разные мощности майнят разные монеты:</b>\n"
                + "\n".join(rigs_lines)
                + f"\n\n🔧 Улучшение фермы до ур. {mining.farm_level + 1}: <b>{upg_str}</b>\n\n"
                f"<i>Команды:</i>\n"
                f"• <code>купить ферму</code> — приобрести базовую ферму\n"
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
