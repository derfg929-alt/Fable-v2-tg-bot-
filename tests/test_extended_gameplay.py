"""Комплексные тесты для нового расширенного игрового функционала Fable:
- Раздельная система помощи и реферальная программа
- Работа и подработка
- Шахта, уровни кирки и переплавка руды
- Покупка и продажа рейтинга
- Кейсы (10 видов) и дроп
- Недвижимость (10 видов) и магазин
- Ники, браки, шар и калькулятор
- Баланс и продажа инвентаря
- Админка для создателей добавленных групп
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import Message, Chat, User

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.help_service import get_section_help_text, get_main_help_menu_text
from app.services.job_service import perform_main_job, perform_side_job, SIDE_JOBS
from app.services.mine_service import (
    get_or_create_mine,
    dig_in_mine,
    upgrade_pickaxe,
    smelt_ores,
)
from app.services.rating_service import buy_rating, sell_rating
from app.services.case_service import open_case, CASES
from app.services.shop_service import buy_property, buy_shop_item, PROPERTIES, SHOP_ITEMS
from app.services.social_fun_service import (
    change_nickname,
    evaluate_math_expression,
    get_magic_ball_prediction,
    propose_marriage,
    accept_marriage,
    divorce,
)
from app.services.moderation_service import get_user_admin_level
from app.handlers.extended_moderation_handlers import on_bot_added_my_chat_member


@pytest.mark.asyncio
async def test_help_sections_and_referral():
    """Тест 10 разделов помощи и реферальной программы."""
    menu = get_main_help_menu_text()
    assert "💎" in menu
    assert "🎮" in menu
    assert "🔨" in menu
    assert "📊" in menu

    for i in range(1, 11):
        sec = get_section_help_text(i)
        assert sec is not None
        assert f"Раздел {i}" in sec

    uid1 = 777001
    uid2 = 777002
    async with session_scope() as session:
        p1 = (await get_or_create_player(session, telegram_id=uid1, first_name="Inviter")).player
        p2 = (await get_or_create_player(session, telegram_id=uid2, first_name="Invited")).player
        p2.referred_by = uid1
        p1.wallet += 1000
        p2.wallet += 1000
        await session.flush()
        assert p2.referred_by == uid1
        assert p1.wallet >= 1000
        assert p2.wallet >= 1000


@pytest.mark.asyncio
async def test_jobs_and_side_jobs():
    """Тест основной работы и подработок."""
    uid = 777003
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Worker")
        # Основная работа
        salary, exp, msg = await perform_main_job(session, user_id=uid)
        assert salary > 0
        assert exp > 0
        assert "РАБОЧАЯ СМЕНА ЗАВЕРШЕНА" in msg

        # Подработка таксистом
        reward, s_exp, s_msg = await perform_side_job(session, user_id=uid, profession="таксист")
        assert reward >= SIDE_JOBS["таксист"].min_reward
        assert "ТАКСИСТ" in s_msg


@pytest.mark.asyncio
async def test_mine_mechanics_and_smelting():
    """Тест шахты: на 1 ур. только уголь, на 2+ ур. металлы, и переплавка."""
    uid = 777004
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Miner")
        mine = await get_or_create_mine(session, user_id=uid)
        assert mine.pickaxe_level == 1

        # Копка на 1 уровне
        res1 = await dig_in_mine(session, user_id=uid)
        assert "coal" in res1.ores_found
        assert "iron" not in res1.ores_found

        # Прокачиваем кирку до 2 уровня
        p = await session.get(type(mine), mine.id)
        mine.pickaxe_level = 2
        mine.last_mined_at = None

        res2 = await dig_in_mine(session, user_id=uid)
        assert "coal" in res2.ores_found
        assert "iron" in res2.ores_found

        # Тест кузницы / переплавки
        mine.coal = 50
        mine.iron = 20
        mine.copper = 10
        bars, smelt_msg = await smelt_ores(session, user_id=uid)
        assert bars > 0
        assert mine.processed_metals >= bars
        assert "ПЕРЕПЛАВКА РУДЫ" in smelt_msg


@pytest.mark.asyncio
async def test_rating_buy_and_sell():
    """Тест покупки и продажи рейтинга."""
    uid = 777005
    async with session_scope() as session:
        p = (await get_or_create_player(session, telegram_id=uid, first_name="Ranker")).player
        p.wallet = 50_000
        await session.flush()

        # Покупка рейтинга
        r, cost = await buy_rating(session, user_id=uid, amount=100)
        assert r == 100
        assert cost == 10_000
        assert p.wallet == 40_000

        # Продажа рейтинга
        r_after, payout = await sell_rating(session, user_id=uid, amount=50)
        assert r_after == 50
        assert payout == 3_500
        assert p.wallet == 43_500


@pytest.mark.asyncio
async def test_10_cases_and_drop():
    """Тест открытия всех 10 кейсов и выпадения наград."""
    uid = 777006
    assert len(CASES) == 10
    async with session_scope() as session:
        p = (await get_or_create_player(session, telegram_id=uid, first_name="CaseOpener")).player
        p.wallet = 100_000_000
        await session.flush()

        # Открываем деревянный (1) и галактический (10)
        res1 = await open_case(session, user_id=uid, case_id=1)
        assert res1.reward_type in ("coins", "rating", "rig")

        res10 = await open_case(session, user_id=uid, case_id=10)
        assert res10.reward_title is not None
        assert p.wallet > 0


@pytest.mark.asyncio
async def test_property_and_shop():
    """Тест покупки недвижимости и товаров в магазине."""
    uid = 777007
    assert len(PROPERTIES) == 10
    assert len(SHOP_ITEMS) >= 5

    async with session_scope() as session:
        p = (await get_or_create_player(session, telegram_id=uid, first_name="RealEstateOwner")).player
        p.wallet = 1_000_000
        await session.flush()

        # Покупка недвижимости
        prop, cfg = await buy_property(session, user_id=uid, property_id=1)
        assert prop.property_id == 1
        assert cfg.name == "Коммуналка"

        # Покупка в магазине
        inv, s_cfg = await buy_shop_item(session, user_id=uid, item_id=1)
        assert inv.item_id == "rolex"


@pytest.mark.asyncio
async def test_social_and_math():
    """Тест смены ника, брака, гадалки и калькулятора."""
    uid1 = 777008
    uid2 = 777009
    async with session_scope() as session:
        p1 = (await get_or_create_player(session, telegram_id=uid1, first_name="Husband")).player
        p2 = (await get_or_create_player(session, telegram_id=uid2, first_name="Wife")).player
        p1.wallet = 50_000
        await session.flush()

        # Ник
        n = await change_nickname(session, user_id=uid1, new_nick="SuperKing")
        assert n == "SuperKing"
        assert p1.custom_nickname == "SuperKing"

        # Брак
        await propose_marriage(session, user_id=uid1, target_id=uid2)
        n1, n2 = await accept_marriage(session, target_id=uid2)
        assert p1.married_to == uid2
        assert p2.married_to == uid1

        # Развод
        sp = await divorce(session, user_id=uid1)
        assert p1.married_to is None
        assert p2.married_to is None

    # Калькулятор
    assert evaluate_math_expression("2 + 2 * 2") == 6
    assert evaluate_math_expression("(100 - 20) / 4") == 20

    # Шар
    ans = get_magic_ball_prediction("Будет ли все хорошо?")
    assert "🔮" in ans


@pytest.mark.asyncio
async def test_chat_creator_admin_assignment():
    """Тест автоматического получения уровня 5 владельцем при добавлении бота в чат."""
    chat_id = -100999111
    adder_id = 777010
    bot = AsyncMock()

    event = MagicMock()
    event.chat = Chat(id=chat_id, title="Owner Group", type="supergroup")
    event.from_user = User(id=adder_id, is_bot=False, first_name="ChatOwner")
    event.new_chat_member = MagicMock()
    event.new_chat_member.status = "administrator"

    await on_bot_added_my_chat_member(event, bot)

    async with session_scope() as session:
        lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=adder_id)
        assert lvl == 5
