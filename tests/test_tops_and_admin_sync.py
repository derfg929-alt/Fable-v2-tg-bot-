import pytest
from unittest.mock import AsyncMock, MagicMock
from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.chat_settings_service import get_or_create_chat
from app.services.moderation_service import (
    get_user_admin_level,
    sync_chat_creator_and_admins,
    issue_warn,
    revoke_warn,
)
from app.services.top_service import (
    get_top_rich,
    get_top_level,
    get_top_rating,
    get_top_mine,
    get_top_businesses,
    get_top_cosmodrome,
)
from app.services.mine_service import get_or_create_mine
from app.services.business_service import buy_business
from app.services.cosmodrome_service import get_or_create_cosmodrome, buy_rocket
from app.handlers.extended_moderation_handlers import (
    on_bot_added_my_chat_member,
    on_bot_added_new_chat_members,
)
from app.handlers.social_moderation_handlers import (
    handle_tops,
    on_top_menu_rich,
    on_top_menu_active,
    on_top_menu_clans,
    on_legacy_top_menu_click,
)


@pytest.mark.asyncio
async def test_bot_added_syncs_chat_owner_level5():
    """Проверяем, что при добавлении бота в чат создатель группы в Telegram автоматически получает ур. 5."""
    chat_id = -10077889900
    creator_id = 77112233
    adder_id = 77112244

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # Мокаем ответ get_chat_administrators
    creator_member = MagicMock()
    creator_member.status = "creator"
    creator_member.user = MagicMock(id=creator_id, first_name="RealOwner", username="owner_tg", is_bot=False)

    admin_member = MagicMock()
    admin_member.status = "administrator"
    admin_member.user = MagicMock(id=adder_id, first_name="AdminGuy", username="admin_tg", is_bot=False)

    mock_bot.get_chat_administrators = AsyncMock(return_value=[creator_member, admin_member])

    # 1. Тест my_chat_member event
    event = MagicMock()
    event.chat = MagicMock(id=chat_id, title="Test Mega Group")
    event.from_user = MagicMock(id=adder_id, first_name="AdminGuy", username="admin_tg", is_bot=False)
    event.new_chat_member = MagicMock(status="administrator")

    await on_bot_added_my_chat_member(event, bot=mock_bot)

    async with session_scope() as session:
        # Проверяем, что реальный создатель получил ур. 5
        owner_lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=creator_id)
        assert owner_lvl == 5

        # Проверяем, что добавивший админ НЕ получает админку автоматически (остается ур. 1)
        adder_lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=adder_id)
        assert adder_lvl == 1


@pytest.mark.asyncio
async def test_all_top_categories():
    """Проверяем получение всех категорий топов (богатые, уровни, рейтинг, шахта, бизнесы, космодром)."""
    u1, u2 = 8811001, 8811002
    async with session_scope() as session:
        reg1 = await get_or_create_player(session, telegram_id=u1, first_name="PlayerOne")
        reg2 = await get_or_create_player(session, telegram_id=u2, first_name="PlayerTwo")
        p1 = reg1.player
        p2 = reg2.player

        p1.wallet = 1_000_000
        p1.experience = 500
        p1.rating = 250

        p2.wallet = 500_000
        p2.experience = 100
        p2.rating = 50

        # Шахта
        m1 = await get_or_create_mine(session, user_id=u1)
        m1.coal = 100
        m1.gold = 10
        m1.processed_metals = 5

        # Бизнес
        await buy_business(session, user_id=u1, business_id=1)

        # Космодром
        c1 = await get_or_create_cosmodrome(session, user_id=u1)
        c1.level = 2
        c1.total_launches = 3
        await buy_rocket(session, user_id=u1, rocket_code="vostok", count=2)

        # 1. Топ богатых
        top_rich = await get_top_rich(session, limit=5)
        assert len(top_rich) >= 2
        assert top_rich[0].user_id == u1

        # 2. Топ уровней / опыта
        top_lvl = await get_top_level(session, limit=5)
        assert len(top_lvl) >= 2
        assert top_lvl[0].user_id == u1
        assert top_lvl[0].experience == 500

        # 3. Топ рейтинга
        top_r = await get_top_rating(session, limit=5)
        assert len(top_r) >= 2
        assert top_r[0].user_id == u1
        assert top_r[0].rating == 250

        # 4. Топ шахты
        top_m = await get_top_mine(session, limit=5)
        assert len(top_m) >= 1
        assert top_m[0].user_id == u1
        assert top_m[0].total_ores >= 110

        # 5. Топ бизнесов
        top_b = await get_top_businesses(session, limit=5)
        assert len(top_b) >= 1
        assert top_b[0].user_id == u1
        assert top_b[0].business_count >= 1

        # 6. Топ космодрома
        top_cosmo = await get_top_cosmodrome(session, limit=5)
        assert len(top_cosmo) >= 1
        assert top_cosmo[0].user_id == u1
        assert top_cosmo[0].rocket_count == 2


@pytest.mark.asyncio
async def test_top_handlers_and_callbacks():
    """Проверяем текстовые команды и нажатия на кнопки категорий топов."""
    # Текстовые команды
    for cmd_text in ("топ", "топ богатых", "топ активных", "топ кланов"):
        msg = MagicMock()
        msg.chat = MagicMock(id=-10012345, type="supergroup", title="SuperChat")
        msg.from_user = MagicMock(id=8811001, first_name="PlayerOne")
        msg.text = cmd_text
        msg.reply = AsyncMock()
        await handle_tops(msg)
        assert msg.reply.called

    # Тестируем callback кнопки категорий
    for cb_func in [on_top_menu_rich, on_top_menu_active, on_top_menu_clans, on_legacy_top_menu_click]:
        cb = MagicMock()
        cb.data = "top_menu_rich"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.chat = MagicMock(id=-10012345, type="supergroup")
        cb.message.edit_text = AsyncMock()
        await cb_func(cb)
        cb.answer.assert_called_once()
        cb.message.edit_text.assert_called_once()

@pytest.mark.asyncio
async def test_profile_displays_admin_level():
    """Проверяем, что в профиле отображается только уровень админки и опыт, а уровень персонажа отсутствует."""
    from app.handlers.economy_handlers import handle_profile
    from app.db.models import ChatAdmin
    u = 88991122
    chat_id = -100998877
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Test Group")
        reg = await get_or_create_player(session, telegram_id=u, first_name="AdminTestUser")
        p = reg.player
        p.experience = 350
        session.add(ChatAdmin(chat_id=chat_id, user_id=u, level=3, assigned_by=u, is_active=True))

    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup")
    msg.from_user = MagicMock(id=u, first_name="AdminTestUser", username="adm_test", last_name=None)
    msg.bot = MagicMock()
    msg.reply = AsyncMock()

    await handle_profile(msg)
    assert msg.reply.called
    sent_text = msg.reply.call_args[0][0]
    assert "Уровень админки: <b>3 (Админ)</b>" in sent_text
    assert "⭐ Уровень:" not in sent_text
