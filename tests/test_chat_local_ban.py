"""Тесты локального (початового) бана Fable.

Проверяют, что бан игрока в одном чате:
- блокирует его только в этом чате;
- не влияет на другие чаты (там доступ и данные сохраняются);
- не удаляет и не обнуляет монеты, бизнесы, фермы и т.д.;
- корректно снимается командой разбана.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from aiogram import types
from aiogram.enums import ChatType
from sqlalchemy import select

from app.db.models import ChatAdmin, ModerationCase, Player, PlayerBusiness
from app.db.session import session_scope
from app.middlewares import MessageActivityAndSecurityMiddleware
from app.services.moderation_service import (
    has_active_chat_ban,
    record_ban_or_kick,
    revoke_ban,
)
from app.services.player_service import get_or_create_player, get_player

MODERATOR_ID = 700700
CHAT_A = -1001001
CHAT_B = -1001002


async def _seed_moderator(session, chat_id: int) -> None:
    from app.services.chat_settings_service import get_or_create_chat

    await get_or_create_chat(session, chat_id=chat_id, title="Group")
    await get_or_create_player(session, telegram_id=MODERATOR_ID, first_name="Mod")
    await session.flush()
    session.add(
        ChatAdmin(
            chat_id=chat_id,
            user_id=MODERATOR_ID,
            level=3,
            assigned_by=MODERATOR_ID,
            is_active=True,
        )
    )
    await session.flush()


async def _make_group_message(uid: int, chat_id: int) -> MagicMock:
    message = MagicMock(spec=types.Message)
    message.from_user = MagicMock(id=uid, is_bot=False, first_name="Guy")
    message.chat = types.Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Group")
    message.text = "баланс"
    message.caption = None
    message.message_id = 1
    message.reply = AsyncMock()
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    return message


async def _run_middleware(message) -> bool:
    handled = False

    async def _handler(event, data):
        nonlocal handled
        handled = True

    mw = MessageActivityAndSecurityMiddleware()
    await mw(_handler, message, {})
    return handled


@pytest.mark.asyncio
async def test_chat_ban_only_affects_that_chat():
    """Бан в чате A блокирует там, но в чате B игрок работает как обычно."""
    uid = 991001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Guy")
        await _seed_moderator(session, CHAT_A)
        await record_ban_or_kick(
            session,
            chat_id=CHAT_A,
            moderator_id=MODERATOR_ID,
            target_id=uid,
            action="ban",
            reason="Спам",
        )

    async with session_scope() as session:
        assert await has_active_chat_ban(session, chat_id=CHAT_A, target_id=uid) is True
        assert await has_active_chat_ban(session, chat_id=CHAT_B, target_id=uid) is False

    # В чате A сообщение удаляется, обработчики не вызываются.
    msg_a = await _make_group_message(uid, CHAT_A)
    handled_a = await _run_middleware(msg_a)
    assert handled_a is False
    assert msg_a.delete.called

    # В чате B бан не действует — сообщение доходит до обработчика.
    msg_b = await _make_group_message(uid, CHAT_B)
    handled_b = await _run_middleware(msg_b)
    assert handled_b is True
    assert not msg_b.delete.called


@pytest.mark.asyncio
async def test_chat_ban_does_not_touch_player_data():
    """Локальный бан не трогает монеты, банк, бизнесы и прочие данные игрока."""
    uid = 991002
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Rich")
        await _seed_moderator(session, CHAT_A)
        player = await get_player(session, uid, for_update=True)
        player.wallet = 12345
        player.bank_balance = 5000
        player.shares = 77
        session.add(PlayerBusiness(user_id=uid, business_id=1, level=3))

    async with session_scope() as session:
        await record_ban_or_kick(
            session,
            chat_id=CHAT_A,
            moderator_id=MODERATOR_ID,
            target_id=uid,
            action="ban",
            reason="Правила",
        )

    async with session_scope() as session:
        player = await get_player(session, uid)
        assert player.wallet == 12345
        assert player.bank_balance == 5000
        assert player.shares == 77
        assert player.is_globally_banned is False
        businesses = (
            await session.scalars(select(PlayerBusiness).where(PlayerBusiness.user_id == uid))
        ).all()
        assert len(businesses) == 1
        assert businesses[0].level == 3


@pytest.mark.asyncio
async def test_revoke_chat_ban_restores_access_in_chat():
    """Разбан снимает локальный бан: игрок снова проходит в чате, данные целы."""
    uid = 991003
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Guy")
        await _seed_moderator(session, CHAT_A)
        await record_ban_or_kick(
            session,
            chat_id=CHAT_A,
            moderator_id=MODERATOR_ID,
            target_id=uid,
            action="ban",
            reason="Флуд",
        )

    async with session_scope() as session:
        revoked = await revoke_ban(
            session,
            chat_id=CHAT_A,
            moderator_id=MODERATOR_ID,
            target_id=uid,
        )
        assert revoked == 1

    async with session_scope() as session:
        assert await has_active_chat_ban(session, chat_id=CHAT_A, target_id=uid) is False

    msg = await _make_group_message(uid, CHAT_A)
    assert await _run_middleware(msg) is True


@pytest.mark.asyncio
async def test_chat_ban_does_not_become_global_ban():
    """Локальный бан никогда не выставляется как глобальный флаг."""
    uid = 991004
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Guy")
        await _seed_moderator(session, CHAT_A)
        await record_ban_or_kick(
            session,
            chat_id=CHAT_A,
            moderator_id=MODERATOR_ID,
            target_id=uid,
            action="ban",
        )

    async with session_scope() as session:
        flag = await session.scalar(
            select(Player.is_globally_banned).where(Player.telegram_id == uid)
        )
        assert flag is False
        # В журнале модерации есть ровно один локальный кейс.
        cases = (
            await session.scalars(
                select(ModerationCase).where(ModerationCase.target_id == uid)
            )
        ).all()
        assert len(cases) == 1
        assert cases[0].chat_id == CHAT_A


@pytest.mark.asyncio
async def test_callback_blocked_only_in_banned_chat():
    """Inline-кнопки игрока блокируются только в том чате, где он забанен."""
    from app.middlewares import CallbackAnswerMiddleware

    uid = 991005
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="Guy")
        await _seed_moderator(session, CHAT_A)
        await record_ban_or_kick(
            session,
            chat_id=CHAT_A,
            moderator_id=MODERATOR_ID,
            target_id=uid,
            action="ban",
        )

    async def _make_callback(chat_id: int) -> MagicMock:
        cb = MagicMock(spec=types.CallbackQuery)
        cb.from_user = MagicMock(id=uid)
        cb.message = MagicMock()
        cb.message.chat = types.Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Group")
        cb.message.delete = AsyncMock()
        cb.answer = AsyncMock()
        return cb

    async def _run(cb) -> bool:
        called = False

        async def _handler(event, data):
            nonlocal called
            called = True

        await CallbackAnswerMiddleware()(_handler, cb, {})
        return called

    # В чате A нажатие блокируется, обработчик не вызывается.
    cb_a = await _make_callback(CHAT_A)
    assert await _run(cb_a) is False
    assert cb_a.answer.called

    # В чате B нажатие работает как обычно.
    cb_b = await _make_callback(CHAT_B)
    assert await _run(cb_b) is True

