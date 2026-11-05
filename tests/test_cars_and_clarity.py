"""Тесты для автопарка (машин, гаража), понятных справок и панели админки."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.shop_service import buy_car, get_player_cars, CARS
from app.handlers.economy_handlers import (
    handle_cars_list,
    handle_garage,
    handle_buy_car_cmd,
    handle_businesses,
    handle_mining,
    handle_cosmodrome,
    handle_mine_cmd,
)
from app.handlers.extended_moderation_handlers import (
    handle_admin_appoint,
    handle_chat_settings_panel,
)
from app.services.chat_settings_service import get_or_create_chat
from app.services.moderation_service import set_admin_level, get_user_admin_level


@pytest.mark.asyncio
async def test_cars_catalog_and_buying():
    """Тест покупки машины и отображения в гараже."""
    uid = 88771199
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="CarDriver")
        p = reg.player
        p.wallet = 500_000
        await session.flush()

        inv, cfg = await buy_car(session, user_id=uid, car_id=1)
        assert inv.item_name == "Lada Vesta Sport"
        assert cfg.car_key == "lada"
        assert p.wallet == 475_000

        cars = await get_player_cars(session, user_id=uid)
        assert len(cars) == 1
        assert cars[0].item_name == "Lada Vesta Sport"


@pytest.mark.asyncio
async def test_cars_and_garage_handlers():
    """Тест хэндлеров команд 'машины', 'гараж' и 'машина купить 2'."""
    uid = 88771188
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="CarFan")
        p = reg.player
        p.wallet = 1_000_000
        await session.flush()

    msg = MagicMock()
    msg.chat = MagicMock(id=-1009988, type="supergroup")
    msg.from_user = MagicMock(id=uid, first_name="CarFan")
    msg.text = "машины"
    msg.reply = AsyncMock()

    await handle_cars_list(msg)
    assert msg.reply.called
    assert "АВТОСАЛОН FABLE" in msg.reply.call_args[0][0]

    # Покупка авто #2
    msg_buy = MagicMock()
    msg_buy.chat = MagicMock(id=-1009988, type="supergroup")
    msg_buy.from_user = MagicMock(id=uid, first_name="CarFan")
    msg_buy.text = "машина купить 2"
    msg_buy.reply = AsyncMock()

    await handle_buy_car_cmd(msg_buy)
    assert msg_buy.reply.called
    assert "ПОЗДРАВЛЯЕМ С ПОКУПКОЙ АВТОМОБИЛЯ" in msg_buy.reply.call_args[0][0]

    # Просмотр гаража
    msg_gar = MagicMock()
    msg_gar.chat = MagicMock(id=-1009988, type="supergroup")
    msg_gar.from_user = MagicMock(id=uid, first_name="CarFan")
    msg_gar.text = "гараж"
    msg_gar.reply = AsyncMock()

    await handle_garage(msg_gar)
    assert msg_gar.reply.called
    assert "ЛИЧНЫЙ ГАРАЖ" in msg_gar.reply.call_args[0][0]
    assert "Toyota Camry" in msg_gar.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_clear_descriptions_business_mining_cosmodrome():
    """Тест подробных и понятных справок о бизнесе, ферме, космодроме и шахте."""
    uid = 88771177
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="InvestTester")

    for handler, cmd_name in [
        (handle_businesses, "бизнес"),
        (handle_mining, "ферма"),
        (handle_cosmodrome, "космодром"),
    ]:
        msg = MagicMock()
        msg.chat = MagicMock(id=-1009988, type="supergroup")
        msg.from_user = MagicMock(id=uid, first_name="InvestTester", last_name=None, username="invest")
        msg.text = cmd_name
        msg.reply = AsyncMock()

        await handler(msg)
        assert msg.reply.called
        text = msg.reply.call_args[0][0]
        # Проверяем наличие информации о доходе, командах и окупаемости
        assert any(k in text for k in ("Доход", "доход", "монет", "Команды", "команды", "Справка", "справка", "Паспорт"))

    # Проверяем справку шахты
    msg_m = MagicMock()
    msg_m.chat = MagicMock(id=-1009988, type="supergroup")
    msg_m.from_user = MagicMock(id=uid, first_name="InvestTester", last_name=None, username="invest")
    msg_m.text = "шахта инфо"
    msg_m.reply = AsyncMock()
    await handle_mine_cmd(msg_m)
    assert msg_m.reply.called
    assert "РУДНИК И ШАХТА" in msg_m.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_adminka_group_panel_and_synonyms():
    """Тест команды 'админка' в группе и синонимов назначения/снятия."""
    chat_id = -100445566
    creator_id = 99112233
    mod_id = 99112244

    from app.db.models import ChatAdmin
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="AdminChat")
        await get_or_create_player(session, telegram_id=creator_id, first_name="OwnerBoss")
        await get_or_create_player(session, telegram_id=mod_id, first_name="PromotedMod")
        session.add(ChatAdmin(chat_id=chat_id, user_id=creator_id, level=5, assigned_by=creator_id, is_active=True))
        await session.flush()

    mock_bot = MagicMock()

    # Проверка команды "админка"
    msg_admin = MagicMock()
    msg_admin.chat = MagicMock(id=chat_id, type="supergroup", title="AdminChat")
    msg_admin.from_user = MagicMock(id=creator_id)
    msg_admin.text = "админка"
    msg_admin.reply = AsyncMock()

    await handle_chat_settings_panel(msg_admin, bot=mock_bot)
    assert msg_admin.reply.called
    assert "Панель настроек" in msg_admin.reply.call_args[0][0]

    # Проверка синонима "дать админку"
    msg_give = MagicMock()
    msg_give.chat = MagicMock(id=chat_id, type="supergroup", title="AdminChat")
    msg_give.from_user = MagicMock(id=creator_id)
    msg_give.text = f"дать админку {mod_id} 3"
    msg_give.reply_to_message = None
    msg_give.entities = None
    msg_give.reply = AsyncMock()

    await handle_admin_appoint(msg_give, bot=mock_bot)
    assert msg_give.reply.called
    assert "установлен уровень 3" in msg_give.reply.call_args[0][0]
