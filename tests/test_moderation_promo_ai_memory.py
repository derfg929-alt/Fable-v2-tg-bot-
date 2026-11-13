"""Тесты новых возможностей Fable:
- Автоматический антиспам (флуд -> авто-мут/бан) и авто-снятие по сроку.
- Логи модерации (лог-канал).
- Фильтр ссылок (по умолчанию выключен; упоминания @user разрешены всегда).
- Капча при входе (по умолчанию выключена, включается командой).
- Промокоды (создание владельцем, активация игроком, лимиты).
- Память ИИ (история диалога, контекст, «забудь»).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.session import session_scope
from app.db.models import (
    AdminActionLog,
    Chat,
    ChatAdmin,
    ModerationCase,
    Player,
)
from app.services.player_service import get_or_create_player
from app.services.chat_settings_service import (
    DEFAULT_CHAT_SETTINGS,
    get_or_create_chat,
    has_pending_captcha,
    create_captcha,
    apply_chat_preset,
)
from app.services.moderation_service import (
    expire_moderation_cases,
    is_link_allowed,
    parse_duration_string,
    issue_mute,
    record_ban_or_kick,
)
from app.services.antispam_service import check_and_apply_antispam
from app.services.logging_service import get_log_channel_id, notify_moderation_log
from app.services.promo_service import (
    activate_promo,
    create_promo,
    delete_promo,
    get_promo,
    list_promos,
    set_promo_active,
    PromoAlreadyUsedError,
    PromoExhaustedError,
    PromoInactiveError,
    PromoNotFoundError,
)
from app.services import ai_memory_service as mem
from app.services.ai_knowledge import build_offline_response
from app.services.ai_service import ask_grok


# ===================== АНТИСПАМ =====================

@pytest.mark.asyncio
async def test_antispam_flood_triggers_auto_mute():
    chat_id = -100700001
    user_id = 7001001
    bot = MagicMock()
    bot.id = 999000111

    settings = {
        "antispam_enabled": True,
        "antispam_window_seconds": 60,
        "antispam_max_messages": 3,
        "antispam_duplicate_threshold": 5,
        "antispam_duration_seconds": 600,
        "antispam_action": "mute",
        "antispam_escalate_after": 3,
    }

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Flood Chat")
        await get_or_create_player(session, telegram_id=user_id, first_name="Flooder")

        results = []
        for i in range(5):
            results.append(await check_and_apply_antispam(
                session, bot=bot, chat_id=chat_id, user_id=user_id,
                text=f"сообщение номер {i}", settings=settings,
            ))
        triggered = [r for r in results if r is not None]
        assert triggered, "антиспам должен сработать при флуде"
        assert triggered[0].action == "mute"
        assert triggered[0].duration == timedelta(seconds=600)

    async with session_scope() as session:
        from sqlalchemy import select
        case = await session.scalar(
            select(ModerationCase).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == user_id,
                ModerationCase.action == "mute",
                ModerationCase.is_active.is_(True),
            )
        )
        assert case is not None


@pytest.mark.asyncio
async def test_antispam_duplicates_trigger_and_escalate_to_ban():
    chat_id = -100700002
    user_id = 7001002
    bot = MagicMock()
    bot.id = 999000111

    settings = {
        "antispam_enabled": True,
        "antispam_window_seconds": 600,
        "antispam_max_messages": 100,  # не по количеству
        "antispam_duplicate_threshold": 3,
        "antispam_duration_seconds": 300,
        "antispam_action": "mute",
        "antispam_escalate_after": 2,  # второе срабатывание -> бан
    }

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Dup Chat")
        await get_or_create_player(session, telegram_id=user_id, first_name="Spammer")
        first = None
        for _ in range(5):
            res = await check_and_apply_antispam(
                session, bot=bot, chat_id=chat_id, user_id=user_id,
                text="одно и то же", settings=settings,
            )
            if res is not None:
                first = res
                break
        assert first is not None and first.action == "mute"

        second = None
        for _ in range(5):
            res = await check_and_apply_antispam(
                session, bot=bot, chat_id=chat_id, user_id=user_id,
                text="опять одно и то же", settings=settings,
            )
            if res is not None:
                second = res
                break
        assert second is not None
        assert second.action == "ban"


@pytest.mark.asyncio
async def test_antispam_disabled_by_default_does_nothing():
    chat_id = -100700003
    user_id = 7001003
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Quiet Chat")
        await get_or_create_player(session, telegram_id=user_id, first_name="User")
        for _ in range(50):
            res = await check_and_apply_antispam(
                session, bot=None, chat_id=chat_id, user_id=user_id,
                text="флуд", settings={"antispam_enabled": False},
            )
            assert res is None


@pytest.mark.asyncio
async def test_antispam_admins_exempt():
    chat_id = -100700004
    admin_id = 7001004
    bot = MagicMock()
    bot.id = 999000111
    settings = {
        "antispam_enabled": True,
        "antispam_window_seconds": 60,
        "antispam_max_messages": 2,
        "antispam_duplicate_threshold": 2,
        "antispam_duration_seconds": 600,
        "antispam_action": "mute",
        "antispam_escalate_after": 3,
    }
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Admin Chat")
        await get_or_create_player(session, telegram_id=admin_id, first_name="Mod")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=3, assigned_by=admin_id, is_active=True))
        await session.flush()
        for _ in range(10):
            res = await check_and_apply_antispam(
                session, bot=bot, chat_id=chat_id, user_id=admin_id,
                text="шум", settings=settings,
            )
            assert res is None


@pytest.mark.asyncio
async def test_expire_moderation_cases_auto_lifts():
    chat_id = -100700005
    target_id = 7001005
    mod_id = 8039111975
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Expire Chat")
        await get_or_create_player(session, telegram_id=target_id, first_name="Target")
        await get_or_create_player(session, telegram_id=mod_id, first_name="Owner")
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.add(ModerationCase(
            chat_id=chat_id, target_id=target_id, moderator_id=mod_id,
            action="mute", reason="temp", expires_at=past, is_active=True,
        ))
        await session.flush()

    async with session_scope() as session:
        lifted = await expire_moderation_cases(session)
        assert lifted >= 1

    async with session_scope() as session:
        from sqlalchemy import select
        active = await session.scalars(
            select(ModerationCase).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "mute",
                ModerationCase.is_active.is_(True),
            )
        )
        assert list(active.all()) == []


# ===================== ФИЛЬТР ССЫЛОК =====================

def test_link_restriction_defaults_off():
    assert DEFAULT_CHAT_SETTINGS["links_restricted"] is False
    assert DEFAULT_CHAT_SETTINGS["captcha_enabled"] is False
    assert DEFAULT_CHAT_SETTINGS["antispam_enabled"] is False


@pytest.mark.asyncio
async def test_usernames_always_allowed_links_blocked():
    chat_id = -100700006
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Links Chat")
        # Упоминание пользователя не является ссылкой
        allowed, _ = await is_link_allowed(session, chat_id=chat_id, text="привет @vasya_pupkin как дела")
        assert allowed is True
        # Обычная ссылка запрещена
        allowed2, found = await is_link_allowed(session, chat_id=chat_id, text="зайди на https://spam.com/x")
        assert allowed2 is False
        assert "spam.com" in found
        # t.me без @ не путается с юзернеймом
        allowed3, _ = await is_link_allowed(session, chat_id=chat_id, text="канал t.me/joinchat/abc")
        assert allowed3 is False


# ===================== КАПЧА =====================

@pytest.mark.asyncio
async def test_captcha_pending_blocks_until_solved():
    chat_id = -100700007
    user_id = 7001007
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Captcha Chat")
        await get_or_create_player(session, telegram_id=user_id, first_name="Newbie")
        assert await has_pending_captcha(session, chat_id=chat_id, user_id=user_id) is False
        await create_captcha(session, chat_id=chat_id, user_id=user_id)
        assert await has_pending_captcha(session, chat_id=chat_id, user_id=user_id) is True


# ===================== ЛОГ-КАНАЛ =====================

@pytest.mark.asyncio
async def test_log_channel_configuration_and_notify():
    chat_id = -100700008
    log_channel = -100999999
    bot = MagicMock()
    bot.send_message = AsyncMock()
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Log Chat")
        chat.log_chat_id = log_channel
        await session.flush()

    async with session_scope() as session:
        found = await get_log_channel_id(session, chat_id=chat_id)
        assert found == log_channel
        ok = await notify_moderation_log(session, bot=bot, chat_id=chat_id, text="тест")
        assert ok is True
    assert bot.send_message.called
    assert bot.send_message.call_args[1]["chat_id"] == log_channel


# ===================== ПРОМОКОДЫ =====================

@pytest.mark.asyncio
async def test_promo_create_and_activate():
    owner = 8039111975
    user = 7002001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=user, first_name="Player")
        promo = await create_promo(session, code="summer", coins=1000, rating=5, created_by=owner)
        assert promo.code == "SUMMER"

    async with session_scope() as session:
        res = await activate_promo(session, code="SUMMER", user_id=user)
        assert res.coins == 1000
        assert res.rating == 5

    async with session_scope() as session:
        p = await session.get(Player, user)
        assert p.wallet == 1000
        assert p.rating == 5
        promo = await get_promo(session, code="SUMMER")
        assert promo.activations == 1


@pytest.mark.asyncio
async def test_promo_per_user_limit():
    user = 7002002
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=user, first_name="Player")
        await create_promo(session, code="ONCE", coins=100, per_user_limit=1)
    async with session_scope() as session:
        await activate_promo(session, code="ONCE", user_id=user)
    with pytest.raises(PromoAlreadyUsedError):
        async with session_scope() as session:
            await activate_promo(session, code="ONCE", user_id=user)


@pytest.mark.asyncio
async def test_promo_max_activations_and_deactivate():
    u1, u2 = 7002003, 7002004
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=u1, first_name="A")
        await get_or_create_player(session, telegram_id=u2, first_name="B")
        await create_promo(session, code="LIMITED", coins=50, max_activations=1)
    async with session_scope() as session:
        await activate_promo(session, code="LIMITED", user_id=u1)
    with pytest.raises(PromoExhaustedError):
        async with session_scope() as session:
            await activate_promo(session, code="LIMITED", user_id=u2)

    async with session_scope() as session:
        await create_promo(session, code="OFFME", coins=10)
        await set_promo_active(session, code="OFFME", active=False)
    with pytest.raises(PromoInactiveError):
        async with session_scope() as session:
            await activate_promo(session, code="OFFME", user_id=u1)


@pytest.mark.asyncio
async def test_promo_not_found_and_delete():
    async with session_scope() as session:
        with pytest.raises(PromoNotFoundError):
            await activate_promo(session, code="NOPE", user_id=1)
    async with session_scope() as session:
        await create_promo(session, code="DELME", coins=10)
        ok = await delete_promo(session, code="DELME")
        assert ok is True
        assert await get_promo(session, code="DELME") is None


@pytest.mark.asyncio
async def test_promo_list():
    async with session_scope() as session:
        await create_promo(session, code="A1", coins=1)
        await create_promo(session, code="A2", coins=2)
        promos = await list_promos(session)
        codes = {p.code for p in promos}
        assert {"A1", "A2"}.issubset(codes)


# ===================== ПАМЯТЬ ИИ =====================

def test_is_forget_request():
    assert mem.is_forget_request("забудь всё") is True
    assert mem.is_forget_request("Фабле, забудь") is True
    assert mem.is_forget_request("расскажи про космос") is False


@pytest.mark.asyncio
async def test_ai_memory_roundtrip_and_context():
    user = 7003001
    chat = 7003001
    async with session_scope() as session:
        await mem.remember_exchange(
            session, user_id=user, chat_id=chat, user_text="Фабле, что такое фотосинтез?",
            assistant_text="Фотосинтез — это процесс...",
        )
        history = await mem.load_history(session, user_id=user, chat_id=chat)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert "фотосинтез" in history[0]["content"].lower()

        ctx = await mem.build_context_string(session, user_id=user, chat_id=chat)
        assert "Игрок:" in ctx
        assert "Fable:" in ctx

    async with session_scope() as session:
        cleared = await mem.clear_history(session, user_id=user, chat_id=chat)
        assert cleared is True
        assert await mem.load_history(session, user_id=user, chat_id=chat) == []


@pytest.mark.asyncio
async def test_ai_memory_per_chat_isolation():
    user = 7003002
    async with session_scope() as session:
        await mem.remember_exchange(session, user_id=user, chat_id=-100, user_text="тема A", assistant_text="ответ A")
        await mem.remember_exchange(session, user_id=user, chat_id=-200, user_text="тема B", assistant_text="ответ B")
    async with session_scope() as session:
        h1 = await mem.load_history(session, user_id=user, chat_id=-100)
        h2 = await mem.load_history(session, user_id=user, chat_id=-200)
        assert h1[0]["content"] == "тема A"
        assert h2[0]["content"] == "тема B"


@pytest.mark.asyncio
async def test_ai_memory_window_trims():
    user = 7003003
    async with session_scope() as session:
        for i in range(40):
            await mem.remember_exchange(
                session, user_id=user, chat_id=user, user_text=f"вопрос {i}", assistant_text=f"ответ {i}",
            )
        history = await mem.load_history(session, user_id=user, chat_id=user)
        assert len(history) <= mem.MAX_HISTORY_MESSAGES


def test_offline_response_uses_context_for_followup():
    answer = build_offline_response("а подробнее?", user_name="Вася", context="Игрок: расскажи про биржу\nFable: биржа — это...")
    assert "продолжаю тему" in answer.lower()
    assert "бирж" in answer.lower()


@pytest.mark.asyncio
async def test_ask_grok_with_context_offline():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.ai_service.resolve_ai_connection", lambda: {
            "provider": None, "api_key": None, "url": None, "model": None, "fallback_models": []
        })
        answer = await ask_grok("а подробнее?", user_name="Имя", context="Игрок: про шахту\nFable: шахта...")
        assert "продолжаю тему" in answer.lower()


# ===================== ДЛИТЕЛЬНОСТИ НАКАЗАНИЙ =====================

def test_requested_durations_parse():
    assert parse_duration_string("1 час") == timedelta(hours=1)
    assert parse_duration_string("2 часа") == timedelta(hours=2)
    assert parse_duration_string("20 часов") == timedelta(hours=20)
    assert parse_duration_string("12 мин") == timedelta(minutes=12)
    assert parse_duration_string("12м") == timedelta(minutes=12)


# ===================== ОБРАБОТЧИКИ (КОМАНДЫ) =====================

def _make_msg(chat_id, user_id, text, chat_type="supergroup"):
    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type=chat_type, title="Test Chat")
    msg.from_user = MagicMock(id=user_id, first_name="Tester", is_bot=False)
    msg.text = text
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_captcha_command_toggles():
    from app.handlers.extended_moderation_handlers import handle_captcha_settings
    chat_id = -100800001
    admin_id = 8039111975  # владелец -> уровень 5
    bot = MagicMock()

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Cap Chat")

    msg = _make_msg(chat_id, admin_id, "+капча")
    await handle_captcha_settings(msg, bot=bot)
    assert msg.reply.called
    async with session_scope() as session:
        chat = await session.get(Chat, chat_id)
        assert chat.settings.get("captcha_enabled") is True

    msg2 = _make_msg(chat_id, admin_id, "-капча")
    await handle_captcha_settings(msg2, bot=bot)
    async with session_scope() as session:
        chat = await session.get(Chat, chat_id)
        assert chat.settings.get("captcha_enabled") is False


@pytest.mark.asyncio
async def test_antispam_command_toggles():
    from app.handlers.extended_moderation_handlers import handle_antispam_settings
    chat_id = -100800002
    admin_id = 8039111975
    bot = MagicMock()
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="AS Chat")

    await handle_antispam_settings(_make_msg(chat_id, admin_id, "антиспам вкл"), bot=bot)
    async with session_scope() as session:
        chat = await session.get(Chat, chat_id)
        assert chat.settings.get("antispam_enabled") is True

    await handle_antispam_settings(_make_msg(chat_id, admin_id, "антиспам срок 1ч"), bot=bot)
    async with session_scope() as session:
        chat = await session.get(Chat, chat_id)
        assert chat.settings.get("antispam_duration_seconds") == 3600

    await handle_antispam_settings(_make_msg(chat_id, admin_id, "антиспам выкл"), bot=bot)
    async with session_scope() as session:
        chat = await session.get(Chat, chat_id)
        assert chat.settings.get("antispam_enabled") is False


@pytest.mark.asyncio
async def test_logs_command_configures_channel():
    from app.handlers.extended_moderation_handlers import handle_moderation_logs
    chat_id = -100800003
    admin_id = 8039111975
    log_channel = -100555000
    bot = MagicMock()
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="LogCfg Chat")

    msg = _make_msg(chat_id, admin_id, f"логи настроить {log_channel}")
    await handle_moderation_logs(msg, bot=bot)
    async with session_scope() as session:
        chat = await session.get(Chat, chat_id)
        assert chat.log_chat_id == log_channel


@pytest.mark.asyncio
async def test_promo_create_and_activate_handlers():
    from app.handlers.promo_handlers import handle_promo_create, handle_promo_activate
    owner = 8039111975
    user = 7009001

    create_msg = _make_msg(owner, owner, "+промо TESTCODE 500 3", chat_type="private")
    await handle_promo_create(create_msg)
    assert create_msg.reply.called
    assert "Промокод создан" in create_msg.reply.call_args[0][0]

    act_msg = _make_msg(user, user, "промо TESTCODE", chat_type="private")
    await handle_promo_activate(act_msg)
    assert act_msg.reply.called
    assert "активирован" in act_msg.reply.call_args[0][0].lower()

    async with session_scope() as session:
        p = await session.get(Player, user)
        assert p.wallet == 500
        assert p.rating == 3


@pytest.mark.asyncio
async def test_promo_activation_duplicate_message():
    from app.handlers.promo_handlers import handle_promo_create, handle_promo_activate
    owner = 8039111975
    user = 7009002
    await handle_promo_create(_make_msg(owner, owner, "+промо DUP 100", chat_type="private"))
    await handle_promo_activate(_make_msg(user, user, "промо DUP", chat_type="private"))
    second = _make_msg(user, user, "промо DUP", chat_type="private")
    await handle_promo_activate(second)
    assert "уже активировали" in second.reply.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_ai_handler_remembers_and_forgets():
    from app.handlers.ai_handlers import handle_ai_query
    from aiogram.enums import ChatType

    user = 7009003
    bot = MagicMock()
    bot.id = 424242
    bot.send_chat_action = AsyncMock()

    # Сообщение с триггером ИИ в ЛС (офлайн-движок, ключа нет)
    msg = MagicMock()
    msg.chat = MagicMock(id=user, type=ChatType.PRIVATE)
    msg.from_user = MagicMock(id=user, first_name="Тестер", is_bot=False)
    msg.text = "Фабле, расскажи про биржу"
    msg.caption = None
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.reply_to_message = None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.ai_service.resolve_ai_connection", lambda: {
            "provider": None, "api_key": None, "url": None, "model": None, "fallback_models": []
        })
        await handle_ai_query(msg, bot=bot)

    async with session_scope() as session:
        hist = await mem.load_history(session, user_id=user, chat_id=user)
        assert len(hist) >= 2
        assert "бирж" in hist[0]["content"].lower()

    # Команда "забудь" очищает память
    forget = MagicMock()
    forget.chat = MagicMock(id=user, type=ChatType.PRIVATE)
    forget.from_user = MagicMock(id=user, first_name="Тестер", is_bot=False)
    forget.text = "Фабле, забудь всё"
    forget.caption = None
    forget.reply = AsyncMock()
    forget.answer = AsyncMock()
    forget.reply_to_message = None
    await handle_ai_query(forget, bot=bot)
    async with session_scope() as session:
        hist = await mem.load_history(session, user_id=user, chat_id=user)
        assert hist == []
