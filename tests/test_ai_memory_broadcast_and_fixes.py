"""Тесты новых возможностей и исправленных багов Fable:

- Память ИИ (контекст диалога, отдельный для ЛС и каждого чата).
- Рассылка владельца не чаще одного раза в 24 часа.
- Админ-панель владельца в ЛС по команде «понель» (без падения types.ChatType).
- Команды «выдать»/«забрать» без AttributeError (current_balance).
- Глобальный бан блокирует экономику.
- Кулдаун космических экспедиций (устранён фарм бесконечных монет).
- Маркетмейкер возвращает резервы и не истощает ликвидность.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.enums import ChatType

from app.db.session import session_scope
from app.services.player_service import get_or_create_player, get_player
from app.services.economy_service import credit_coins
from app.services.ai_memory_service import (
    remember_exchange,
    get_conversation_history,
    clear_conversation,
)
from app.services.ai_knowledge import build_offline_response
from app.services.owner_service import (
    get_broadcast_cooldown,
    mark_broadcast_sent,
    list_broadcast_chats,
    BROADCAST_COOLDOWN_SECONDS,
)
from app.services.cosmodrome_service import (
    buy_rocket,
    launch_rocket_mission,
    CosmodromeError,
)
from app.services.market_maker import run_market_maker_cycle, get_or_create_mm_state
from app.handlers.owner_handlers import (
    _build_admin_panel_payload,
    handle_admin_panel,
    handle_give_coins,
    handle_take_coins,
)
from app.middlewares import MessageActivityAndSecurityMiddleware

OWNER_ID = 8039111975


# ===================== ПАМЯТЬ ИИ =====================


@pytest.mark.asyncio
async def test_ai_memory_roundtrip_and_chat_isolation():
    """Память хранит историю отдельно для ЛС и каждого чата."""
    uid = 77000001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="MemoryTester")
        await remember_exchange(
            session, user_id=uid, chat_id=0,
            user_message="Что такое фотосинтез?", assistant_message="Фотосинтез — процесс...",
        )
        await remember_exchange(
            session, user_id=uid, chat_id=-100500,
            user_message="Как играть в слоты?", assistant_message="Слоты: ставка...",
        )

    async with session_scope() as session:
        pm_history = await get_conversation_history(session, user_id=uid, chat_id=0)
        chat_history = await get_conversation_history(session, user_id=uid, chat_id=-100500)

    assert len(pm_history) == 2
    assert pm_history[0]["role"] == "user"
    assert "фотосинтез" in pm_history[0]["content"].lower()
    assert len(chat_history) == 2
    assert "слоты" in chat_history[0]["content"].lower()
    # Истории не смешиваются между ЛС и чатом.
    assert pm_history != chat_history


@pytest.mark.asyncio
async def test_ai_memory_clear():
    """Команда сброса памяти очищает историю."""
    uid = 77000002
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="ClearTester")
        await remember_exchange(
            session, user_id=uid, chat_id=0,
            user_message="Привет", assistant_message="Привет!",
        )
    async with session_scope() as session:
        assert await clear_conversation(session, user_id=uid, chat_id=0) is True
        assert await get_conversation_history(session, user_id=uid, chat_id=0) == []


def test_offline_followup_uses_context():
    """Уточняющий вопрос («а подробнее?») опирается на прошлую тему диалога."""
    context = "Игрок: Что такое квантовая запутанность?\nFable: Квантовая запутанность — ..."
    answer = build_offline_response("а подробнее?", user_name="Игрок", context=context)
    assert "запутанность" in answer.lower()
    # Без контекста уточняющий вопрос не должен выдумывать тему.
    no_ctx = build_offline_response("а подробнее?", user_name="Игрок", context=None)
    assert "продолжаю по теме" not in no_ctx.lower()


# ===================== РАССЫЛКА (1 РАЗ В 24 ЧАСА) =====================


@pytest.mark.asyncio
async def test_broadcast_cooldown_24h():
    """Рассылка разрешена только раз в 24 часа."""
    owner = OWNER_ID
    async with session_scope() as session:
        can_send, remaining = await get_broadcast_cooldown(session, owner_id=owner)
        assert can_send is True
        assert remaining == 0

    async with session_scope() as session:
        await mark_broadcast_sent(session, owner_id=owner)

    async with session_scope() as session:
        can_send2, remaining2 = await get_broadcast_cooldown(session, owner_id=owner)
        assert can_send2 is False
        assert 0 < remaining2 <= BROADCAST_COOLDOWN_SECONDS


@pytest.mark.asyncio
async def test_broadcast_cooldown_expires():
    """Спустя 24 часа рассылка снова доступна."""
    from datetime import timedelta

    owner = OWNER_ID
    past = datetime.now(timezone.utc) - timedelta(hours=25)
    async with session_scope() as session:
        await mark_broadcast_sent(session, owner_id=owner, now=past)
    async with session_scope() as session:
        can_send, remaining = await get_broadcast_cooldown(session, owner_id=owner)
    assert can_send is True
    assert remaining == 0


@pytest.mark.asyncio
async def test_list_broadcast_chats_only_active_groups():
    """В список рассылки попадают только активные группы."""
    from app.db.models import Chat

    async with session_scope() as session:
        session.add(Chat(telegram_id=-1001, title="Активная группа", chat_type="supergroup", is_active=True))
        session.add(Chat(telegram_id=-1002, title="Отключённая группа", chat_type="supergroup", is_active=False))
        session.add(Chat(telegram_id=-1003, title="Приватный чат", chat_type="private", is_active=True))

    async with session_scope() as session:
        chats = await list_broadcast_chats(session, owner_id=OWNER_ID)
    ids = {c.telegram_id for c in chats}
    assert -1001 in ids
    assert -1002 not in ids
    assert -1003 not in ids


# ===================== АДМИН-ПАНЕЛЬ ВЛАДЕЛЬЦА В ЛС =====================


@pytest.mark.asyncio
async def test_admin_panel_builds_without_crash():
    """Главная админ-панель владельца собирается без ошибок."""
    text, markup = await _build_admin_panel_payload(OWNER_ID)
    assert "АДМИН-ПАНЕЛЬ ВЛАДЕЛЬЦА" in text
    assert "рассылка" in text.lower()


@pytest.mark.asyncio
async def test_admin_panel_dm_command_works():
    """Команда «понель» в ЛС владельца отвечает панелью (проверка фикса types.ChatType)."""
    message = MagicMock()
    message.from_user = MagicMock(id=OWNER_ID)
    message.chat = types.Chat(id=OWNER_ID, type=ChatType.PRIVATE)
    message.reply = AsyncMock()

    await handle_admin_panel(message)

    assert message.reply.called
    text = message.reply.call_args[0][0]
    assert "АДМИН-ПАНЕЛЬ ВЛАДЕЛЬЦА" in text


@pytest.mark.asyncio
async def test_admin_panel_ignored_in_group():
    """В группе панель владельца не перехватывает управление."""
    message = MagicMock()
    message.from_user = MagicMock(id=OWNER_ID)
    message.chat = types.Chat(id=-100999, type=ChatType.SUPERGROUP)
    message.reply = AsyncMock()

    await handle_admin_panel(message)
    message.reply.assert_not_called()


# ===================== ВЫДАТЬ / ЗАБРАТЬ =====================


@pytest.mark.asyncio
async def test_owner_give_and_take_coins_no_attributeerror():
    """Команды выдачи/списания монет работают без AttributeError."""
    target = 77000003
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=target, first_name="Target")

    give_msg = MagicMock()
    give_msg.from_user = MagicMock(id=OWNER_ID)
    give_msg.text = f"выдать {target} 500"
    give_msg.reply = AsyncMock()
    await handle_give_coins(give_msg)
    assert give_msg.reply.called
    assert "500" in give_msg.reply.call_args[0][0]

    async with session_scope() as session:
        p = await get_player(session, target)
        assert p.wallet == 500

    take_msg = MagicMock()
    take_msg.from_user = MagicMock(id=OWNER_ID)
    take_msg.text = f"забрать {target} 200"
    take_msg.reply = AsyncMock()
    await handle_take_coins(take_msg)
    assert take_msg.reply.called

    async with session_scope() as session:
        p = await get_player(session, target)
        assert p.wallet == 300


# ===================== ГЛОБАЛЬНЫЙ БАН =====================


@pytest.mark.asyncio
async def test_global_ban_blocks_economy_messages():
    """Забаненный игрок не может пользоваться экономикой в ЛС."""
    uid = 77000004
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="Banned")
        reg.player.is_globally_banned = True

    message = MagicMock(spec=types.Message)
    message.from_user = MagicMock(id=uid, is_bot=False)
    message.chat = types.Chat(id=uid, type=ChatType.PRIVATE)
    message.text = "баланс"
    message.reply = AsyncMock()

    handled = False

    async def _handler(event, data):
        nonlocal handled
        handled = True

    mw = MessageActivityAndSecurityMiddleware()
    await mw(_handler, message, {})

    assert handled is False
    assert message.reply.called


@pytest.mark.asyncio
async def test_global_ban_allows_normal_user():
    """Незабаненный игрок проходит дальше в обработчики."""
    uid = 77000005
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Normal")

    message = MagicMock(spec=types.Message)
    message.from_user = MagicMock(id=uid, is_bot=False)
    message.chat = types.Chat(id=uid, type=ChatType.PRIVATE)
    message.text = "баланс"

    handled = False

    async def _handler(event, data):
        nonlocal handled
        handled = True

    mw = MessageActivityAndSecurityMiddleware()
    await mw(_handler, message, {})
    assert handled is True


# ===================== КУЛДАУН КОСМОДРОМА =====================


@pytest.mark.asyncio
async def test_cosmodrome_launch_has_cooldown():
    """Повторный запуск ракеты в течение 30 минут запрещён (нет фарма бесконечных монет)."""
    uid = 77000006
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="Cosmo")
        reg.player.wallet = 1_000_000
        await session.flush()
        await buy_rocket(session, user_id=uid, rocket_code="vostok", count=1)

    async with session_scope() as session:
        first = await launch_rocket_mission(session, user_id=uid, rocket_code="vostok")
        assert first.reward_coins > 0

    with pytest.raises(CosmodromeError):
        async with session_scope() as session:
            await launch_rocket_mission(session, user_id=uid, rocket_code="vostok")


# ===================== МАРКЕТМЕЙКЕР =====================


@pytest.mark.asyncio
async def test_market_maker_reserves_not_depleted():
    """Маркетмейкер возвращает резервы и не истощает ликвидность между циклами."""
    async with session_scope() as session:
        await run_market_maker_cycle(session)
    async with session_scope() as session:
        state = await get_or_create_mm_state(session)
        reserve_after_first = state.coin_reserve
    async with session_scope() as session:
        await run_market_maker_cycle(session)
    async with session_scope() as session:
        state2 = await get_or_create_mm_state(session)
        reserve_after_second = state2.coin_reserve

    # Резерв не должен уменьшаться при повторном цикле (старые заявки возвращаются).
    assert reserve_after_second >= reserve_after_first


# ===================== ЭКСПЛОЙТ БАЙТ-ДРОПА =====================


@pytest.mark.asyncio
async def test_bait_claim_is_idempotent():
    """Один дроп нельзя забрать дважды (нет фарма бесконечных монет)."""
    from app.handlers.games_handlers import handle_bait_claim

    user = types.User(id=88000001, is_bot=False, first_name="Hunter")
    chat = types.Chat(id=-100500, type=ChatType.SUPERGROUP, title="Bait")
    message = AsyncMock(spec=types.Message)
    message.chat = chat
    message.message_id = 777
    message.edit_text = AsyncMock()

    callback = AsyncMock(spec=types.CallbackQuery)
    callback.from_user = user
    callback.message = message
    callback.data = "bait:claim"
    callback.answer = AsyncMock()

    await handle_bait_claim(callback)
    async with session_scope() as session:
        p1 = await get_player(session, user.id)
        assert p1.wallet == 25

    # Повторное нажатие той же кнопки не начисляет монеты снова.
    await handle_bait_claim(callback)
    async with session_scope() as session:
        p2 = await get_player(session, user.id)
        assert p2.wallet == 25


# ===================== ВЫДАТЬ/ЗАБРАТЬ РЕГРЕССИЯ =====================


@pytest.mark.asyncio
async def test_give_coins_creates_player_when_missing():
    """Выдача монет новому (не зарегистрированному) игроку не падает."""
    target = 88000002
    msg = MagicMock()
    msg.from_user = MagicMock(id=OWNER_ID)
    msg.text = f"выдать {target} 100"
    msg.reply = AsyncMock()

    await handle_give_coins(msg)
    assert msg.reply.called
    async with session_scope() as session:
        p = await get_player(session, target)
        assert p is not None
        assert p.wallet == 100
