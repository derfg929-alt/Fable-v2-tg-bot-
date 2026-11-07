import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from app.db.session import session_scope
from app.services.player_service import get_or_create_player, format_time_in_fable
from app.services.chat_settings_service import get_or_create_chat
from app.services.moderation_service import (
    get_user_admin_level,
    set_admin_level,
    issue_mute,
    revoke_mute,
    issue_warn,
    revoke_warn,
    get_active_warns,
    record_ban_or_kick,
    sync_chat_creator_and_admins,
    get_chat_admins,
)
from app.handlers.economy_handlers import handle_profile
from app.handlers.social_moderation_handlers import (
    handle_mute,
    handle_unmute,
    handle_warn,
    handle_view_warns,
    handle_admin_list,
)
from app.handlers.extended_moderation_handlers import (
    handle_ban,
    handle_unban,
    handle_kick,
)


def test_format_time_in_fable():
    now = datetime.now(timezone.utc)
    t1 = now - timedelta(days=5, hours=3, minutes=12)
    res = format_time_in_fable(t1)
    assert "5 д." in res
    assert "3 ч." in res
    assert "12 мин." in res

    t2 = now - timedelta(minutes=45)
    res2 = format_time_in_fable(t2)
    assert "45 мин." in res2


@pytest.mark.asyncio
async def test_whois_and_profile_commands():
    chat_id = -10055443322
    u1 = 81110001
    u2 = 81110002

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Whois Chat")
        p1 = (await get_or_create_player(session, telegram_id=u1, first_name="TargetUser")).player
        p2 = (await get_or_create_player(session, telegram_id=u2, first_name="ViewerUser")).player
        p1.created_at = datetime.now(timezone.utc) - timedelta(days=2, hours=1)
        await session.flush()

    # 1. Проверяем команду 'кто ты' по ответу на сообщение (reply)
    reply_msg = MagicMock()
    reply_msg.from_user = MagicMock(id=u1, first_name="TargetUser", username="target_tg", last_name=None)

    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="Whois Chat")
    msg.from_user = MagicMock(id=u2, first_name="ViewerUser", username="viewer_tg", last_name=None)
    msg.reply_to_message = reply_msg
    msg.text = "кто ты"
    msg.reply = AsyncMock()

    await handle_profile(msg)
    assert msg.reply.called
    text = msg.reply.call_args[0][0]
    assert "Профиль игрока TargetUser" in text
    assert "🔔 Звонок:" in text
    assert "Первое появление в боте:" in text
    assert "Активность в «Whois Chat»:" in text
    assert "За день:" in text
    assert "За всё время:" in text

    # 2. Проверяем команду 'профиль' для самого себя
    msg_self = MagicMock()
    msg_self.chat = MagicMock(id=chat_id, type="supergroup", title="Whois Chat")
    msg_self.from_user = MagicMock(id=u2, first_name="ViewerUser", username="viewer_tg", last_name=None)
    msg_self.reply_to_message = None
    msg_self.text = "профиль"
    msg_self.reply = AsyncMock()

    await handle_profile(msg_self)
    assert msg_self.reply.called
    text_self = msg_self.reply.call_args[0][0]
    assert "Профиль игрока ViewerUser" in text_self
    assert "🔔 Звонок:" in text_self
    assert "Первое появление в боте:" in text_self
    assert "Активность в «Whois Chat»:" in text_self


@pytest.mark.asyncio
async def test_admins_sync_creator_only_level5():
    chat_id = -10055443323
    creator_id = 81110003
    admin_id = 81110004

    mock_bot = MagicMock()
    c_member = MagicMock()
    c_member.status = "creator"
    c_member.user = MagicMock(id=creator_id, first_name="TrueCreator", username="true_creator", is_bot=False)

    a_member = MagicMock()
    a_member.status = "administrator"
    a_member.user = MagicMock(id=admin_id, first_name="JustAdmin", username="just_admin", is_bot=False)

    mock_bot.get_chat_administrators = AsyncMock(return_value=[c_member, a_member])

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Creator Only Chat")
        found_creator = await sync_chat_creator_and_admins(session, chat_id=chat_id, bot=mock_bot)
        assert found_creator == creator_id

        # Создатель получил уровень 5
        lvl_creator = await get_user_admin_level(session, chat_id=chat_id, user_id=creator_id, bot=mock_bot)
        assert lvl_creator == 5

        # Админ Telegram НЕ получил уровень 3 или 5, а остался ур. 1
        lvl_admin = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id, bot=mock_bot)
        assert lvl_admin == 1


@pytest.mark.asyncio
async def test_moderation_warns_counter_and_admins_list():
    chat_id = -10055443324
    creator_id = 81110005
    target_id = 81110006

    mock_bot = MagicMock()
    mock_bot.restrict_chat_member = AsyncMock(return_value=True)
    mock_bot.ban_chat_member = AsyncMock(return_value=True)
    mock_bot.unban_chat_member = AsyncMock(return_value=True)

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Mod Chat")
        await get_or_create_player(session, telegram_id=creator_id, first_name="CreatorAdmin")
        await get_or_create_player(session, telegram_id=target_id, first_name="BadBoy")
        # Назначаем создателя
        from app.db.models import ChatAdmin
        session.add(ChatAdmin(chat_id=chat_id, user_id=creator_id, level=5, assigned_by=creator_id, is_active=True))
        await session.flush()

    # 1. Проверяем выдачу варна
    reply_msg = MagicMock()
    reply_msg.from_user = MagicMock(id=target_id, first_name="BadBoy", is_bot=False)

    warn_msg = MagicMock()
    warn_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    warn_msg.from_user = MagicMock(id=creator_id, first_name="CreatorAdmin", is_bot=False)
    warn_msg.reply_to_message = reply_msg
    warn_msg.text = "варн Флуд в чате"
    warn_msg.bot = mock_bot
    warn_msg.reply = AsyncMock()

    await handle_warn(warn_msg)
    assert warn_msg.reply.called
    assert "(1/5)" in warn_msg.reply.call_args[0][0]

    # 2. Выдаем второй варн
    warn_msg.text = "варн Оскорбления"
    await handle_warn(warn_msg)
    assert "(2/5)" in warn_msg.reply.call_args[0][0]

    # 3. Проверяем команду 'варны'
    view_warns_msg = MagicMock()
    view_warns_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    view_warns_msg.from_user = MagicMock(id=creator_id, first_name="CreatorAdmin", is_bot=False)
    view_warns_msg.reply_to_message = reply_msg
    view_warns_msg.text = "варны"
    view_warns_msg.reply = AsyncMock()

    await handle_view_warns(view_warns_msg)
    assert view_warns_msg.reply.called
    warns_text = view_warns_msg.reply.call_args[0][0]
    assert "Счётчик варнов: 2/5" in warns_text
    assert "Флуд в чате" in warns_text
    assert "Оскорбления" in warns_text

    # 4. Проверяем команду 'админы'
    admins_msg = MagicMock()
    admins_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    admins_msg.from_user = MagicMock(id=target_id, first_name="BadBoy", is_bot=False)
    admins_msg.text = "админы"
    admins_msg.reply = AsyncMock()

    await handle_admin_list(admins_msg)
    assert admins_msg.reply.called
    admins_text = admins_msg.reply.call_args[0][0]
    assert "Администрация чата «Mod Chat»" in admins_text
    assert "CreatorAdmin" in admins_text
    assert "Ранг 5 (Создатель)" in admins_text

    # 5. Проверяем снятие варна
    unwarn_msg = MagicMock()
    unwarn_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    unwarn_msg.from_user = MagicMock(id=creator_id, first_name="CreatorAdmin", is_bot=False)
    unwarn_msg.reply_to_message = reply_msg
    unwarn_msg.text = "снять варн"
    unwarn_msg.bot = mock_bot
    unwarn_msg.reply = AsyncMock()

    await handle_warn(unwarn_msg)
    assert unwarn_msg.reply.called
    assert "Предупреждение снято" in unwarn_msg.reply.call_args[0][0]

    # 6. Проверяем мут и размут
    mute_msg = MagicMock()
    mute_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    mute_msg.from_user = MagicMock(id=creator_id, first_name="CreatorAdmin", is_bot=False)
    mute_msg.reply_to_message = reply_msg
    mute_msg.text = "мут 10м Спам"
    mute_msg.bot = mock_bot
    mute_msg.reply = AsyncMock()

    await handle_mute(mute_msg)
    assert mute_msg.reply.called
    assert "ПОЛЬЗОВАТЕЛЬ ЗАМЬЮЧЕН!" in mute_msg.reply.call_args[0][0]

    unmute_msg = MagicMock()
    unmute_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    unmute_msg.from_user = MagicMock(id=creator_id, first_name="CreatorAdmin", is_bot=False)
    unmute_msg.reply_to_message = reply_msg
    unmute_msg.text = "размут"
    unmute_msg.bot = mock_bot
    unmute_msg.reply = AsyncMock()

    await handle_unmute(unmute_msg)
    assert unmute_msg.reply.called
    assert "МУТ СНЯТ!" in unmute_msg.reply.call_args[0][0]

    # 7. Проверяем бан и разбан
    ban_msg = MagicMock()
    ban_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    ban_msg.from_user = MagicMock(id=creator_id, first_name="CreatorAdmin", is_bot=False)
    ban_msg.reply_to_message = reply_msg
    ban_msg.text = "бан Реклама"
    ban_msg.reply = AsyncMock()

    await handle_ban(ban_msg, bot=mock_bot)
    assert ban_msg.reply.called
    assert "ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН (БАН)!" in ban_msg.reply.call_args[0][0]

    unban_msg = MagicMock()
    unban_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Mod Chat")
    unban_msg.from_user = MagicMock(id=creator_id, first_name="CreatorAdmin", is_bot=False)
    unban_msg.reply_to_message = reply_msg
    unban_msg.text = "разбан"
    unban_msg.reply = AsyncMock()

    await handle_unban(unban_msg, bot=mock_bot)
    assert unban_msg.reply.called
    assert "ПОЛЬЗОВАТЕЛЬ РАЗБЛОКИРОВАН!" in unban_msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_moderation_unseen_target_does_not_fail_foreign_key():
    """Тестируем, что мут пользователя, которого никогда не было в БД, не вызывает ForeignKeyViolationError."""
    chat_id = -1003982804242
    moderator_id = 8039111975
    unseen_target_id = 707693258  # Пользователь из сообщения об ошибке

    mock_bot = MagicMock()
    mock_bot.restrict_chat_member = AsyncMock(return_value=True)

    target_user_mock = MagicMock()
    target_user_mock.id = unseen_target_id
    target_user_mock.first_name = "NewbieUser"
    target_user_mock.username = "newbie_user"
    mock_member = MagicMock(user=target_user_mock)
    mock_bot.get_chat_member = AsyncMock(return_value=mock_member)

    reply_msg = MagicMock()
    reply_msg.from_user = target_user_mock

    mute_msg = MagicMock()
    mute_msg.chat = MagicMock(id=chat_id, type="supergroup", title="Foreign Key Test Chat")
    mute_msg.from_user = MagicMock(id=moderator_id, first_name="OwnerMod", is_bot=False)
    mute_msg.reply_to_message = reply_msg
    mute_msg.text = "мут 30м Нарушение порядка"
    mute_msg.bot = mock_bot
    mute_msg.reply = AsyncMock()

    # До вызова убеждаемся, что в базе игрока с unseen_target_id нет
    from app.db.models import Player, ModerationCase
    async with session_scope() as session:
        existing_p = await session.get(Player, unseen_target_id)
        if existing_p:
            await session.delete(existing_p)
            await session.flush()

    await handle_mute(mute_msg)
    assert mute_msg.reply.called
    reply_text = mute_msg.reply.call_args[0][0]
    assert "ПОЛЬЗОВАТЕЛЬ ЗАМЬЮЧЕН!" in reply_text
    assert str(unseen_target_id) in reply_text

    # Проверяем, что запись в moderation_cases успешно создана и foreign key удовлетворен
    async with session_scope() as session:
        from sqlalchemy import select
        case = await session.scalar(
            select(ModerationCase).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == unseen_target_id,
                ModerationCase.action == "mute",
            )
        )
        assert case is not None
        assert case.moderator_id == moderator_id
        assert case.target_id == unseen_target_id


@pytest.mark.asyncio
async def test_callback_answer_middleware():
    """Тестируем CallbackAnswerMiddleware, гарантирующий ответ на нажатие кнопки."""
    from app.middlewares import CallbackAnswerMiddleware
    from aiogram.types import CallbackQuery

    middleware = CallbackAnswerMiddleware()
    cb = MagicMock(spec=CallbackQuery)
    cb.answer = AsyncMock()

    handler_called = False

    async def sample_handler(event, data):
        nonlocal handler_called
        handler_called = True
        return "success"

    res = await middleware(sample_handler, cb, {})
    assert res == "success"
    assert handler_called
    assert cb.answer.called
