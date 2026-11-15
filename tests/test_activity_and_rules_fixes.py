"""Тесты исправлений: награда за активность (случайные монеты, без опыта),
отсутствие системы опыта и управление правилами чата."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from aiogram import types
from aiogram.enums import ChatType

from app.db.models import ActivityRecord, ChatAdmin, Player
from app.db.session import session_scope
from app.services.player_service import get_or_create_player, get_player
from app.services.chat_settings_service import get_or_create_chat, get_chat_rules
from app.services.activity_service import (
    get_activity_settings,
    reward_chat_message,
    set_activity_settings,
)
from app.handlers.social_moderation_handlers import handle_rules_manage
from app.handlers.extended_moderation_handlers import handle_antiflood_pause


CHAT_ID = -100700100


@pytest.mark.asyncio
async def test_player_model_has_no_experience_column():
    """Система опыта полностью удалена из модели игрока."""
    assert "experience" not in Player.__table__.columns
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=700001, first_name="NoXP")
        assert not hasattr(reg.player, "experience")


@pytest.mark.asyncio
async def test_activity_reward_is_random_1_to_50():
    """Награда за сообщение — случайная величина в диапазоне 1..coins_per_message."""
    chat_id = CHAT_ID + 1
    uid = 700002
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Active")
        await get_or_create_chat(session, chat_id=chat_id, title="Activity")
        await set_activity_settings(session, values={"coins_per_message": 50, "daily_coin_limit": 100000})

        settings = await get_activity_settings(session)
        assert settings["coins_per_message"] == 50

        amounts = []
        for i in range(40):
            moment = datetime.now(timezone.utc) + timedelta(seconds=i * 120)
            res = await reward_chat_message(
                session,
                chat_id=chat_id,
                user_id=uid,
                message_id=1000 + i,
                text=f"уникальное сообщение номер {i}",
                now=moment,
            )
            if res.awarded:
                amounts.append(res.coins)

        assert amounts, "Ни одна награда не была выдана"
        assert all(1 <= c <= 50 for c in amounts)
        # При 40 попытках значения не должны быть все одинаковыми.
        assert len(set(amounts)) > 1


@pytest.mark.asyncio
async def test_activity_reward_respects_cooldown_and_idempotency():
    """Кулдаун и идемпотентность по message_id работают (повтор не начисляет)."""
    chat_id = CHAT_ID + 2
    uid = 700003
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Cooldown")
        await get_or_create_chat(session, chat_id=chat_id, title="Activity2")
        await set_activity_settings(session, values={"coins_per_message": 50, "cooldown_seconds": 60})

        moment = datetime.now(timezone.utc)
        first = await reward_chat_message(
            session, chat_id=chat_id, user_id=uid, message_id=1, text="первое сообщение тут", now=moment
        )
        assert first.awarded

        # Повтор того же message_id — идемпотентность.
        again = await reward_chat_message(
            session, chat_id=chat_id, user_id=uid, message_id=1, text="первое сообщение тут", now=moment
        )
        assert not again.awarded
        assert again.reason == "already_processed"

        # Новое сообщение сразу — кулдаун.
        fast = await reward_chat_message(
            session,
            chat_id=chat_id,
            user_id=uid,
            message_id=2,
            text="второе сообщение другое",
            now=moment + timedelta(seconds=5),
        )
        assert not fast.awarded
        assert fast.reason == "cooldown"


@pytest.mark.asyncio
async def test_rules_add_and_delete_flow():
    """Команды управления правилами: добавление и удаление (ур. 3+)."""
    chat_id = CHAT_ID + 3
    admin_id = 700004
    chat = types.Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Rules Chat")

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=admin_id, first_name="RulesAdmin")
        await get_or_create_chat(session, chat_id=chat_id, title="Rules Chat")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=5, assigned_by=admin_id, is_active=True))

    add_msg = MagicMock()
    add_msg.chat = chat
    add_msg.from_user = MagicMock(id=admin_id, first_name="RulesAdmin")
    add_msg.bot = MagicMock()
    add_msg.text = "правило добавить Не спамить в чате"
    add_msg.reply = AsyncMock()

    await handle_rules_manage(add_msg)
    assert add_msg.reply.called
    assert "добавлено" in add_msg.reply.call_args[0][0]

    async with session_scope() as session:
        rules = await get_chat_rules(session, chat_id=chat_id)
        assert len(rules) == 1
        assert rules[0].text == "Не спамить в чате"

    del_msg = MagicMock()
    del_msg.chat = chat
    del_msg.from_user = MagicMock(id=admin_id, first_name="RulesAdmin")
    del_msg.bot = MagicMock()
    del_msg.text = "правило удалить 1"
    del_msg.reply = AsyncMock()

    await handle_rules_manage(del_msg)
    assert del_msg.reply.called
    assert "удалено" in del_msg.reply.call_args[0][0]

    async with session_scope() as session:
        rules = await get_chat_rules(session, chat_id=chat_id)
        assert len(rules) == 0


@pytest.mark.asyncio
async def test_rules_manage_requires_admin():
    """Обычный участник не может управлять правилами."""
    chat_id = CHAT_ID + 4
    user_id = 700005
    chat = types.Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Rules Chat 2")

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=user_id, first_name="Plain")
        await get_or_create_chat(session, chat_id=chat_id, title="Rules Chat 2")

    msg = MagicMock()
    msg.chat = chat
    msg.from_user = MagicMock(id=user_id, first_name="Plain")
    msg.bot = MagicMock()
    msg.text = "правило добавить Нельзя"
    msg.reply = AsyncMock()

    await handle_rules_manage(msg)
    assert msg.reply.called
    assert "ур. 3+" in msg.reply.call_args[0][0]

    async with session_scope() as session:
        rules = await get_chat_rules(session, chat_id=chat_id)
        assert len(rules) == 0


@pytest.mark.asyncio
async def test_antiflood_pause_bare_number_is_seconds():
    """Голое число в «антифлуд пауза N» трактуется как секунды, а не минуты."""
    chat_id = CHAT_ID + 5
    admin_id = 700006
    chat = types.Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Flood Chat")

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=admin_id, first_name="FloodAdmin")
        await get_or_create_chat(session, chat_id=chat_id, title="Flood Chat")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=5, assigned_by=admin_id, is_active=True))

    msg = MagicMock()
    msg.chat = chat
    msg.from_user = MagicMock(id=admin_id, first_name="FloodAdmin")
    msg.bot = MagicMock()
    msg.text = "антифлуд пауза 5"
    msg.reply = AsyncMock()

    await handle_antiflood_pause(msg, bot=MagicMock())
    assert msg.reply.called

    async with session_scope() as session:
        settings = (await get_or_create_chat(session, chat_id=chat_id)).settings
        assert settings["antiflood_pause_seconds"] == 5
