import pytest
from unittest.mock import AsyncMock, MagicMock
from app.db.session import session_scope, create_database_engine
from app.services.player_service import get_or_create_player
from app.services.chat_settings_service import get_or_create_chat, toggle_ai_moderation, set_ai_moderation_strictness
from app.services.moderation_service import get_user_admin_level, sync_chat_creator_and_admins
from app.handlers.games_handlers import handle_games_menu, handle_crash, handle_shooting_range
from app.handlers.economy_handlers import handle_rating_info_cmd, handle_achievements_cmd
from app.handlers.extended_moderation_handlers import handle_chat_settings_panel
from app.handlers.ai_handlers import _should_trigger_ai


@pytest.mark.asyncio
async def test_postgresql_config_and_engine():
    """Проверка конфигурации PostgreSQL URL и параметров движка asyncpg."""
    from app.config import Settings
    # Создаем экземпляр настроек с дефолтными значениями без переопределения через os.environ
    settings_default = Settings(_env_file=None, database_url="postgresql+asyncpg://fable:fable@localhost:5432/fable")
    assert "postgresql+asyncpg" in settings_default.database_url_value

    # Проверка создания движка под PostgreSQL
    pg_engine = create_database_engine("postgresql://fable:fable@localhost:5432/fable")
    assert "postgresql+asyncpg" in str(pg_engine.url)
    await pg_engine.dispose()


@pytest.mark.asyncio
async def test_chat_creator_gets_max_level_5():
    """Владелец чата при добавлении бота и проверке получает максимальный уровень 5 (Создатель)."""
    chat_id = -100998877
    creator_id = 77112233
    mock_bot = MagicMock()

    # Мокаем ответ Telegram get_chat_administrators
    creator_member = MagicMock()
    creator_member.status = "creator"
    creator_member.user = MagicMock(id=creator_id, first_name="ChatCreator", username="creator_boss", is_bot=False)
    mock_bot.get_chat_administrators = AsyncMock(return_value=[creator_member])
    mock_bot.get_chat_member = AsyncMock(return_value=creator_member)

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="SupremeChat")
        # Синхронизация администраторов чата
        found_creator = await sync_chat_creator_and_admins(session, chat_id=chat_id, bot=mock_bot)
        assert found_creator == creator_id

        # Проверяем уровень Создателя
        lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=creator_id, bot=mock_bot)
        assert lvl == 5


@pytest.mark.asyncio
async def test_chat_settings_panel_and_owner_rights():
    """Тест работы админ-панели для создателей чата с уровнем 5."""
    chat_id = -100554433
    creator_id = 88223344

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="TestGroup")
        await get_or_create_player(session, telegram_id=creator_id, first_name="Boss")
        from app.db.models import ChatAdmin
        session.add(ChatAdmin(chat_id=chat_id, user_id=creator_id, level=5, assigned_by=creator_id, is_active=True))
        await session.flush()

    mock_bot = MagicMock()
    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="TestGroup")
    msg.from_user = MagicMock(id=creator_id)
    msg.text = "админка"
    msg.reply = AsyncMock()

    await handle_chat_settings_panel(msg, bot=mock_bot)
    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    assert "Панель настроек" in reply_text
    assert "Создатель (ур. 5)" in reply_text


@pytest.mark.asyncio
async def test_all_new_and_previously_missing_commands():
    """Проверка команд, на которые бот ранее не реагировал: рейтинг, казино, достижения, краш, тир."""
    user_id = 99887711
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=user_id, first_name="GamerPlayer")
        reg.player.wallet = 100_000
        reg.player.rating = 50

    # 1. Команда "рейтинг"
    msg_rate = MagicMock()
    msg_rate.from_user = MagicMock(id=user_id, first_name="GamerPlayer")
    msg_rate.text = "рейтинг"
    msg_rate.reply = AsyncMock()
    await handle_rating_info_cmd(msg_rate)
    assert msg_rate.reply.called
    assert "ИГРОВОЙ РЕЙТИНГ" in msg_rate.reply.call_args[0][0]

    # 2. Команда "казино" / "игры"
    msg_games = MagicMock()
    msg_games.from_user = MagicMock(id=user_id)
    msg_games.text = "казино"
    msg_games.reply = AsyncMock()
    await handle_games_menu(msg_games)
    assert msg_games.reply.called
    assert "ИГРОВОЙ ЗАЛ И КАЗИНО FABLE" in msg_games.reply.call_args[0][0]

    # 3. Команда "достижения" / "квесты"
    msg_ach = MagicMock()
    msg_ach.from_user = MagicMock(id=user_id, first_name="GamerPlayer")
    msg_ach.text = "достижения"
    msg_ach.reply = AsyncMock()
    await handle_achievements_cmd(msg_ach)
    assert msg_ach.reply.called
    assert "ИГРОВЫЕ ДОСТИЖЕНИЯ" in msg_ach.reply.call_args[0][0]

    # 4. Игра "краш 100"
    msg_crash = MagicMock()
    msg_crash.from_user = MagicMock(id=user_id, first_name="GamerPlayer")
    msg_crash.text = "краш 100"
    msg_crash.reply = AsyncMock()
    await handle_crash(msg_crash)
    assert msg_crash.reply.called
    assert "ИГРА КРАШ" in msg_crash.reply.call_args[0][0]

    # 5. Игра "тир 100"
    msg_shoot = MagicMock()
    msg_shoot.from_user = MagicMock(id=user_id, first_name="GamerPlayer")
    msg_shoot.text = "тир 100"
    msg_shoot.reply = AsyncMock()
    await handle_shooting_range(msg_shoot)
    assert msg_shoot.reply.called
    assert "СТРЕЛКОВЫЙ ТИР" in msg_shoot.reply.call_args[0][0]
