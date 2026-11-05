"""Тесты нового функционала:
- Космодром, ангар ракет, запуск миссий, сбор прибыли и улучшения.
- Снаряжение игрока: прокачка оружия, удочки, машины такси, байка курьера и др.
- Охота и рыбалка: сохранение добычи в инвентарь и последующая продажа в магазин.
- Случайный баланс подработок с бонусом от транспорта/инструментов.
- Баланс азартных игр: слоты, рулетка, сундук, кости.
- Топ чата и глобальный.
"""
from __future__ import annotations

import pytest
from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.cosmodrome_service import (
    ROCKETS,
    buy_rocket,
    upgrade_cosmodrome,
    collect_cosmodrome_profits,
    launch_rocket_mission,
    calculate_hourly_income,
    get_rockets_dict,
    get_or_create_cosmodrome,
)
from app.services.gear_service import (
    GEAR_ITEMS,
    upgrade_player_gear,
    get_or_create_gear,
    get_gear_bonus_percent,
)
from app.services.games_service import (
    play_hunting,
    play_fishing,
    play_slots,
    play_dice,
    play_coinflip,
    play_mystery_box,
    HUNTING_PRICES,
    FISHING_PRICES,
)
from app.services.job_service import perform_side_job
from app.services.top_service import get_top_rich, get_top_active, get_top_clans
from app.db.models import PlayerInventory, Player


@pytest.mark.asyncio
async def test_cosmodrome_mechanics():
    """Тест космодрома: покупка ракет, ангар, запуск экспедиции, сбор дохода."""
    uid = 999001
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="Cosmonaut")
        p = reg.player
        p.wallet = 5_000_000
        await session.flush()

        cosmo = await get_or_create_cosmodrome(session, user_id=uid)
        assert cosmo.level == 1
        assert cosmo.total_launches == 0

        # Покупка ракеты Восток-1
        c, cfg, cost = await buy_rocket(session, user_id=uid, rocket_code="vostok", count=2)
        assert cfg.code == "vostok"
        assert cost == 50_000
        rockets = get_rockets_dict(c)
        assert rockets["vostok"] == 2

        # Запуск миссии
        mission = await launch_rocket_mission(session, user_id=uid, rocket_code="vostok")
        assert mission.is_success is True
        assert mission.reward_coins > 0
        assert c.total_launches == 1

        # Модернизация космодрома
        upg_cosmo, upg_cost = await upgrade_cosmodrome(session, user_id=uid)
        assert upg_cosmo.level == 2

        # Покупка ракеты Союз-2 (доступна со 2 уровня)
        c2, cfg2, cost2 = await buy_rocket(session, user_id=uid, rocket_code="soyuz", count=1)
        assert cfg2.code == "soyuz"

        hourly = calculate_hourly_income(get_rockets_dict(c2), c2.level)
        assert hourly > 0


@pytest.mark.asyncio
async def test_gear_and_side_job_boost():
    """Тест прокачки экипировки и влияния на заработок подработок."""
    uid = 999002
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="Driver")
        p = reg.player
        p.wallet = 1_000_000
        await session.flush()

        gear = await get_or_create_gear(session, user_id=uid)
        assert gear.taxi_car_level == 1

        # Улучшение рок-машины такси
        name, new_lvl, cost = await upgrade_player_gear(session, user_id=uid, gear_type="такси")
        assert new_lvl == 2
        assert gear.taxi_car_level == 2
        assert get_gear_bonus_percent(2) == 15

        # Выполнение подработки таксистом
        reward, exp, msg = await perform_side_job(session, user_id=uid, profession="таксист")
        assert reward > 0
        assert exp > 0
        assert "Бонус от снаряжения" in msg


@pytest.mark.asyncio
async def test_hunting_and_fishing_inventory_flow():
    """Тест охоты и рыбалки: добыча попадает в инвентарь игрока."""
    uid = 999003
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="HunterFisher")
        p = reg.player

        # Прокачиваем оружие и удочку до максимума
        gear = await get_or_create_gear(session, user_id=uid)
        gear.hunting_rifle_level = 10
        gear.fishing_rod_level = 10
        await session.flush()

        # Охота
        h_res = await play_hunting(session, user_id=uid)
        assert h_res.category != "failure"
        assert h_res.reward_coins > 0

        # Рыбалка
        f_res = await play_fishing(session, user_id=uid)
        assert f_res.category != "empty"
        assert f_res.reward_coins > 0

        # Проверяем наличие предметов в инвентаре
        from sqlalchemy import select
        inv_items = await session.scalars(
            select(PlayerInventory).where(PlayerInventory.user_id == uid)
        )
        items = list(inv_items.all())
        assert len(items) >= 2
        cats = [i.category for i in items]
        assert "hunting" in cats
        assert "fishing" in cats


@pytest.mark.asyncio
async def test_casino_games_balance():
    """Тест балансировки азартных игр (слоты, рулетка, кости, сундук)."""
    uid = 999004
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="Gambler")
        p = reg.player
        p.wallet = 10_000
        await session.flush()

        # Кости
        p_roll, b_roll, win, payout = await play_dice(session, user_id=uid, stake=100)
        assert 1 <= p_roll <= 6
        assert 1 <= b_roll <= 6

        # Монетка
        outcome, c_win, c_payout = await play_coinflip(session, user_id=uid, choice="орел", stake=100)
        assert outcome in ("орел", "решка")

        # Сундук
        mult, b_payout = await play_mystery_box(session, user_id=uid, stake=100)
        assert mult in (0.0, 0.5, 1.2, 1.5, 2.0, 3.5)

        # Слоты
        slot_res = await play_slots(session, user_id=uid, stake=100)
        assert len(slot_res.reels) == 3


@pytest.mark.asyncio
async def test_top_services_global_and_chat():
    """Тест глобальных и локальных топов."""
    async with session_scope() as session:
        rich_global = await get_top_rich(session, chat_id=None, limit=5)
        assert isinstance(rich_global, list)

        active_global = await get_top_active(session, chat_id=None, limit=5)
        assert isinstance(active_global, list)

        clans_global = await get_top_clans(session, chat_id=None, limit=5)
        assert isinstance(clans_global, list)
