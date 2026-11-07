from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram import types
from aiogram.enums import ChatType

from app.db.session import session_scope
from app.db.models import Player, ChatAdmin
from app.services.player_service import get_or_create_player, format_time_in_bot
from app.services.chat_settings_service import get_or_create_chat
from app.handlers.economy_handlers import handle_profile
from app.handlers.social_moderation_handlers import handle_mute, handle_unmute, handle_warn, handle_admin_list
from app.handlers.extended_moderation_handlers import handle_ban, handle_unban


@pytest.mark.asyncio
async def test_format_time_in_bot():
    now = datetime.now(timezone.utc)
    assert "мин" in format_time_in_bot(now - timedelta(minutes=5))
    assert "ч" in format_time_in_bot(now - timedelta(hours=3))
    assert "вчера" == format_time_in_bot(now - timedelta(days=1))
    assert "дн" in format_time_in_bot(now - timedelta(days=4))
    assert "нед" in format_time_in_bot(now - timedelta(days=14))
    assert "мес" in format_time_in_bot(now - timedelta(days=60))
    assert "г" in format_time_in_bot(now - timedelta(days=400))


@pytest.mark.asyncio
async def test_profile_and_kto_ty():
    user1 = 777111001
    user2 = 777111002
    chat_id = -100777111000

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Test Moderation Chat")
        reg1 = await get_or_create_player(session, telegram_id=user1, first_name="TargetUser", username="target_user")
        p1 = reg1.player
        p1.registered_at = datetime.now(timezone.utc) - timedelta(days=32)

        reg2 = await get_or_create_player(session, telegram_id=user2, first_name="ViewerUser", username="viewer_user")
        session.add(ChatAdmin(chat_id=chat_id, user_id=user1, level=2, assigned_by=user1, is_active=True))

    # 1. Просмотр своего профиля
    msg_self = MagicMock()
    msg_self.chat = MagicMock(id=chat_id, type="supergroup")
    msg_self.from_user = MagicMock(id=user1, first_name="TargetUser", username="target_user", last_name=None)
    msg_self.text = "профиль"
    msg_self.bot = MagicMock()
    msg_self.reply = AsyncMock()

    await handle_profile(msg_self)
    msg_self.reply.assert_called_once()
    self_text = msg_self.reply.call_args[0][0]
    assert "🛡 Ранг: <b>2 (Модератор)</b>" in self_text
    assert "⏳ В боте: <b>1 мес. назад</b>" in self_text
    assert "⭐ Рейтинг:" not in self_text
    assert "✨ Опыт:" not in self_text

    # 2. Просмотр чужого профиля через "кто ты" по @username
    msg_other = MagicMock()
    msg_other.chat = MagicMock(id=chat_id, type="supergroup")
    msg_other.from_user = MagicMock(id=user2, first_name="ViewerUser", username="viewer_user", last_name=None)
    msg_other.text = "кто ты @target_user"
    msg_other.reply_to_message = None
    msg_other.entities = []
    msg_other.bot = MagicMock()
    msg_other.reply = AsyncMock()

    await handle_profile(msg_other)
    msg_other.reply.assert_called_once()
    other_text = msg_other.reply.call_args[0][0]
    assert "Профиль игрока TargetUser" in other_text
    assert f"🆔 ID: <code>{user1}</code>" in other_text
    assert "🛡 Ранг: <b>2 (Модератор)</b>" in other_text


@pytest.mark.asyncio
async def test_moderation_mute_warn_ban_reply_and_username():
    admin_id = 8039111975  # Владелец / уровень 5
    target_id = 998877665
    chat_id = -100554433221

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Mod Chat")
        await get_or_create_player(session, telegram_id=admin_id, first_name="AdminOwner", username="admin_owner")
        reg_target = await get_or_create_player(session, telegram_id=target_id, first_name="Spammer", username="spammer_usr")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=5, assigned_by=admin_id, is_active=True))

    # Мут по ответу на сообщение (просто 'мут 10м флуд')
    msg_mute = MagicMock()
    msg_mute.chat = MagicMock(id=chat_id, type=ChatType.SUPERGROUP)
    msg_mute.from_user = MagicMock(id=admin_id, first_name="AdminOwner")
    msg_mute.text = "мут 10м флуд"
    msg_mute.entities = []
    msg_mute.reply_to_message = MagicMock(from_user=MagicMock(id=target_id))
    msg_mute.bot = AsyncMock()
    msg_mute.reply = AsyncMock()

    await handle_mute(msg_mute)
    msg_mute.reply.assert_called_once()
    mute_text = msg_mute.reply.call_args[0][0]
    assert "ПОЛЬЗОВАТЕЛЬ ЗАМЬЮЧЕН" in mute_text
    assert f"<code>{target_id}</code>" in mute_text

    # Варн по @username ('варн @spammer_usr Оскорбление')
    msg_warn = MagicMock()
    msg_warn.chat = MagicMock(id=chat_id, type=ChatType.SUPERGROUP)
    msg_warn.from_user = MagicMock(id=admin_id, first_name="AdminOwner")
    msg_warn.text = "варн @spammer_usr Оскорбление"
    msg_warn.entities = []
    msg_warn.reply_to_message = None
    msg_warn.bot = AsyncMock()
    msg_warn.reply = AsyncMock()

    await handle_warn(msg_warn)
    msg_warn.reply.assert_called_once()
    warn_text = msg_warn.reply.call_args[0][0]
    assert "выдано предупреждение" in warn_text
    assert f"<code>{target_id}</code>" in warn_text

    # Бан по ID ('бан 998877665 Читер')
    msg_ban = MagicMock()
    msg_ban.chat = MagicMock(id=chat_id, type=ChatType.SUPERGROUP)
    msg_ban.from_user = MagicMock(id=admin_id, first_name="AdminOwner")
    msg_ban.text = f"бан {target_id} Читер"
    msg_ban.entities = []
    msg_ban.reply_to_message = None
    msg_ban.bot = AsyncMock()
    msg_ban.reply = AsyncMock()

    await handle_ban(msg_ban)
    msg_ban.reply.assert_called_once()
    ban_text = msg_ban.reply.call_args[0][0]
    assert "ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН" in ban_text
    assert f"<code>{target_id}</code>" in ban_text

    # Проверка команды "админы"
    msg_admins = MagicMock()
    msg_admins.chat = MagicMock(id=chat_id, type=ChatType.SUPERGROUP, title="Mod Chat")
    msg_admins.from_user = MagicMock(id=admin_id)
    msg_admins.text = "админы"
    msg_admins.bot = AsyncMock()
    msg_admins.reply = AsyncMock()

    await handle_admin_list(msg_admins)
    msg_admins.reply.assert_called_once()
    admins_text = msg_admins.reply.call_args[0][0]
    assert "Администрация чата" in admins_text
    assert "Ранг" in admins_text
