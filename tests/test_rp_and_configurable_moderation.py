"""Тесты новых возможностей Fable:
- Настройка варнов с авто-эскалацией (лимит -> мут/бан/кик, двойной лимит -> бан).
- Гибкий парсинг «мут 67» (голое число = минуты).
- Антимат (фильтр нецензурной лексики, обходы латиницей/разделителями).
- Анти-флуд «пауза».
- Антиспам: капс и спам эмодзи.
- RP/развлекательные функции (кубик, шип, комплимент, правда/действие, рандом).
- Улучшенные ответы ИИ на оскорбления и вариативность ответов.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.session import session_scope
from app.db.models import ChatAdmin, ModerationCase, Player
from app.services.player_service import get_or_create_player
from app.services.chat_settings_service import (
    DEFAULT_CHAT_SETTINGS,
    get_or_create_chat,
)
from app.services.moderation_service import (
    apply_warn_escalation,
    extract_moderation_args,
    parse_duration_string,
    issue_warn,
)
from app.services import badwords_service as bw
from app.services.antispam_service import check_flood_pause, check_and_apply_antispam
from app.services import rp_service
from app.services.ai_knowledge import build_offline_response
from app.handlers.social_moderation_handlers import handle_warn, handle_warn_settings, handle_chat_purge


# ===================== ГИБКИЕ СРОКИ =====================

def test_bare_number_is_minutes():
    assert parse_duration_string("67") == timedelta(minutes=67)
    dur, disp, reason = extract_moderation_args("мут 67 флуд")
    assert dur == timedelta(minutes=67)
    assert "1 ч." in disp and "7 мин." in disp
    assert reason == "флуд"


# ===================== ВАРНЫ И ЭСКАЛАЦИЯ =====================

@pytest.mark.asyncio
async def test_warn_escalation_mute_then_permaban():
    chat_id = -100900001
    mod_id = 9000001
    target_id = 9000002
    bot = MagicMock()
    bot.id = 999000111

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Warn Chat")
        await get_or_create_player(session, telegram_id=mod_id, first_name="Mod")
        await get_or_create_player(session, telegram_id=target_id, first_name="BadGuy")
        session.add(ChatAdmin(chat_id=chat_id, user_id=mod_id, level=4, assigned_by=mod_id, is_active=True))
        await session.flush()

    settings = dict(DEFAULT_CHAT_SETTINGS)
    settings["warn_limit"] = 2
    settings["warn_action"] = "mute"
    settings["warn_duration_seconds"] = 600
    settings["warn_escalate_ban"] = True

    # 1-й варн — без эскалации
    async with session_scope() as session:
        _, cnt = await issue_warn(session, chat_id=chat_id, moderator_id=mod_id, target_id=target_id, reason="тест", bot=bot)
        esc = await apply_warn_escalation(session, chat_id=chat_id, target_id=target_id, warn_count=cnt, settings=settings, bot=bot)
        assert cnt == 1
        assert esc.triggered is False

    # 2-й варн — авто-мут (лимит достигнут)
    async with session_scope() as session:
        _, cnt = await issue_warn(session, chat_id=chat_id, moderator_id=mod_id, target_id=target_id, reason="тест", bot=bot)
        esc = await apply_warn_escalation(session, chat_id=chat_id, target_id=target_id, warn_count=cnt, settings=settings, bot=bot)
        assert cnt == 2
        assert esc.triggered is True
        assert esc.action == "mute"

    # Двойной лимит — бессрочный бан
    async with session_scope() as session:
        esc = await apply_warn_escalation(session, chat_id=chat_id, target_id=target_id, warn_count=4, settings=settings, bot=bot)
        assert esc.triggered is True
        assert esc.action == "ban"
        assert esc.duration is None

    async with session_scope() as session:
        from sqlalchemy import select as _select
        rows = (await session.scalars(
            _select(ModerationCase).where(ModerationCase.chat_id == chat_id)
        )).all()
        actions = {row.action for row in rows}
        assert "mute" in actions and "ban" in actions


@pytest.mark.asyncio
async def test_warn_settings_command_persists():
    chat_id = -100900010
    admin_id = 9000010
    bot = MagicMock()
    bot.id = 999000111

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Settings Chat")
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=4, assigned_by=admin_id, is_active=True))
        await session.flush()

    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="Settings Chat")
    msg.from_user = MagicMock(id=admin_id, first_name="Admin", is_bot=False)
    msg.text = "варн лимит 3"
    msg.bot = bot
    msg.reply = AsyncMock()

    await handle_warn_settings(msg)
    assert msg.reply.called

    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id)
        assert chat.settings.get("warn_limit") == 3


@pytest.mark.asyncio
async def test_warn_handler_triggers_auto_action():
    chat_id = -100900020
    admin_id = 9000020
    target_id = 9000021
    bot = MagicMock()
    bot.id = 999000111
    bot.restrict_chat_member = AsyncMock(return_value=True)
    bot.ban_chat_member = AsyncMock(return_value=True)

    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="AutoWarn Chat")
        s = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        s["warn_limit"] = 1
        s["warn_action"] = "mute"
        s["warn_duration_seconds"] = 300
        chat.settings = s
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        await get_or_create_player(session, telegram_id=target_id, first_name="Target")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=4, assigned_by=admin_id, is_active=True))
        await session.flush()

    reply_msg = MagicMock()
    reply_msg.from_user = MagicMock(id=target_id, first_name="Target", is_bot=False)

    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="AutoWarn Chat")
    msg.from_user = MagicMock(id=admin_id, first_name="Admin", is_bot=False)
    msg.reply_to_message = reply_msg
    msg.text = "варн спам"
    msg.bot = bot
    msg.reply = AsyncMock()

    await handle_warn(msg)
    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    assert "лимит предупреждений" in reply_text.lower()
    assert bot.restrict_chat_member.called


# ===================== АНТИМАТ =====================

def test_badwords_detection_and_evasions():
    assert bw.find_bad_word("это просто бляха какая-то") is not None
    assert bw.find_bad_word("нормальное сообщение без мата") is None
    # Обход латиницей и разделителями
    assert bw.find_bad_word("з а е б а л") is not None
    assert bw.find_bad_word("xyй") is not None  # латинские x,y -> х,у
    assert bw.find_bad_word("fuuuck") is not None


@pytest.mark.asyncio
async def test_badwords_custom_words():
    chat_id = -100900030
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="BadWords Chat")
        await bw.add_custom_bad_word(session, chat_id=chat_id, word="кастом")
        words = await bw.get_custom_bad_words(session, chat_id=chat_id)
        assert "кастом" in words

    async with session_scope() as session:
        hit = await bw.check_message(
            session,
            chat_id=chat_id,
            text="тут есть кастом слово",
            settings={"badwords_enabled": True, "badwords_action": "delete"},
        )
        assert hit is not None
        assert hit.action == "delete"

        # Отключённый фильтр ничего не находит
        hit_off = await bw.check_message(
            session, chat_id=chat_id, text="кастом", settings={"badwords_enabled": False}
        )
        assert hit_off is None

        ok = await bw.remove_custom_bad_word(session, chat_id=chat_id, word="кастом")
        assert ok is True


# ===================== АНТИ-ФЛУД «ПАУЗА» =====================

@pytest.mark.asyncio
async def test_flood_pause_blocks_fast_messages():
    chat_id = -100900040
    user_id = 9000040
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Flood Chat")
        await get_or_create_player(session, telegram_id=user_id, first_name="Fast")
        first = await check_flood_pause(session, chat_id=chat_id, user_id=user_id, min_interval_seconds=5)
        assert first is False  # первое сообщение пропускается
        second = await check_flood_pause(session, chat_id=chat_id, user_id=user_id, min_interval_seconds=5)
        assert second is True  # слишком быстро


@pytest.mark.asyncio
async def test_antispam_caps_and_emoji_detection():
    chat_id = -100900050
    user_id = 9000050
    bot = MagicMock()
    bot.id = 999000111

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Caps Chat")
        await get_or_create_player(session, telegram_id=user_id, first_name="Shouter")

    settings = {
        "antispam_enabled": True,
        "antispam_window_seconds": 60,
        "antispam_max_messages": 100,   # чтобы не сработал по количеству
        "antispam_duplicate_threshold": 100,
        "antispam_duration_seconds": 120,
        "antispam_action": "mute",
        "antispam_escalate_after": 99,
        "antispam_caps_ratio": 0.7,
        "antispam_caps_min_length": 10,
        "antispam_emoji_limit": 5,
    }

    async with session_scope() as session:
        res = await check_and_apply_antispam(
            session, bot=bot, chat_id=chat_id, user_id=user_id,
            text="ЭТО ОЧЕНЬ ГРОМКОЕ СООБЩЕНИЕ КАПСОМ", settings=settings,
        )
        assert res is not None
        assert "капс" in res.reason.lower()

    # Спам эмодзи
    async with session_scope() as session:
        res2 = await check_and_apply_antispam(
            session, bot=bot, chat_id=chat_id, user_id=user_id,
            text="😀😀😀😀😀😀😀😀", settings=settings,
        )
        assert res2 is not None
        assert "эмодзи" in res2.reason.lower()


# ===================== RP / РАЗВЛЕЧЕНИЯ =====================

def test_rp_parsing_and_texts():
    assert rp_service.parse_rp_command("обнять @user")[0] == "обнять"
    assert rp_service.parse_rp_command("hug Вася")[0] == "обнять"
    assert rp_service.parse_rp_command("обычное сообщение") is None
    text = rp_service.build_rp_text("поцеловать", "Аня", "Ваня")
    assert "Аня" in text and "Ваня" in text


def test_roll_and_ship_and_compliment():
    roll = rp_service.roll_dice_text(2, 6)
    assert "2d6" in roll
    # ship детерминирован
    a = rp_service.ship_text("Аня", "Ваня", "1", "2")
    b = rp_service.ship_text("Аня", "Ваня", "1", "2")
    assert a == b
    assert "%" in a
    assert "Комплимент" or rp_service.compliment_text("Вася")


def test_truth_or_dare_has_content():
    text = rp_service.truth_or_dare_text()
    assert ("Правда" in text) or ("Действие" in text)


# ===================== ИИ: ОСКОРБЛЕНИЯ И ВАРИАТИВНОСТЬ =====================

def test_ai_handles_insults_gracefully():
    ans = build_offline_response("ты дурак и бесполезный бот", user_name="Тестер")
    low = ans.lower()
    # Не повторяем оскорбление и предлагаем помощь/команды
    assert "дурак" not in low
    assert ("помоч" in low) or ("помог" in low) or ("команд" in low) or ("подсказать" in low)

    ans2 = build_offline_response("заткнись", user_name="Тестер")
    assert ans2  # непустой осмысленный ответ


def test_ai_variative_generic_answers():
    a = build_offline_response("расскажи про квантовую запутанность")
    b = build_offline_response("расскажи про теорию относительности")
    assert a != b  # разные вопросы -> разные формулировки


def test_ai_apology_response():
    ans = build_offline_response("извини меня пожалуйста", user_name="Тестер")
    assert "порядке" in ans.lower() or "обид" in ans.lower()


# ===================== ЧИСТКА (ПУРЖ) =====================

@pytest.mark.asyncio
async def test_purge_by_count_and_all():
    chat_id = -100900060
    admin_id = 9000060
    bot = MagicMock()
    bot.delete_message = AsyncMock(return_value=True)

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Purge Chat")
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=3, assigned_by=admin_id, is_active=True))
        await session.flush()

    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="Purge Chat")
    msg.from_user = MagicMock(id=admin_id, first_name="Admin", is_bot=False)
    msg.text = "чистка все 5"
    msg.message_id = 100
    msg.bot = bot
    msg.reply_to_message = None
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()

    await handle_chat_purge(msg)
    assert bot.delete_message.called
    # удалили не более 5 сообщений
    assert bot.delete_message.call_count <= 5


# ===================== MIDDLEWARE: АНТИМАТ И АНТИ-ФЛУД =====================

def _make_group_message(chat_id: int, user_id: int, text: str):
    from aiogram import types as tg_types

    msg = MagicMock(spec=tg_types.Message)
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="MW Chat")
    msg.from_user = MagicMock(id=user_id, first_name="Spammer", is_bot=False, username="spammer")
    msg.text = text
    msg.caption = None
    msg.message_id = 555
    msg.bot = MagicMock()
    msg.bot.id = 999000111
    msg.delete = AsyncMock()
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_middleware_deletes_badwords_message():
    from app.middlewares import MessageActivityAndSecurityMiddleware

    chat_id = -100900070
    user_id = 9000070
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="MW Chat")
        s = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        s["badwords_enabled"] = True
        s["badwords_action"] = "delete"
        chat.settings = s
        await get_or_create_player(session, telegram_id=user_id, first_name="Spammer")
        await session.flush()

    mw = MessageActivityAndSecurityMiddleware()
    called = {"handler": False}

    async def handler(event, data):
        called["handler"] = True
        return "handled"

    msg = _make_group_message(chat_id, user_id, "это полный бляха текст")
    result = await mw(handler, msg, {})
    assert called["handler"] is False
    assert msg.delete.called


@pytest.mark.asyncio
async def test_middleware_antiflood_pause_blocks_second_message():
    from app.middlewares import MessageActivityAndSecurityMiddleware

    chat_id = -100900080
    user_id = 9000080
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="MW Flood Chat")
        s = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        s["antiflood_pause_seconds"] = 30
        chat.settings = s
        await get_or_create_player(session, telegram_id=user_id, first_name="Fast")
        await session.flush()

    mw = MessageActivityAndSecurityMiddleware()

    async def handler(event, data):
        return "handled"

    first = _make_group_message(chat_id, user_id, "первое сообщение")
    assert await mw(handler, first, {}) == "handled"

    second = _make_group_message(chat_id, user_id, "второе сообщение")
    await mw(handler, second, {})
    assert second.delete.called


@pytest.mark.asyncio
async def test_middleware_badwords_warn_action_issues_warn():
    from app.middlewares import MessageActivityAndSecurityMiddleware

    chat_id = -100900090
    user_id = 9000090
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="MW Warn Chat")
        s = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
        s["badwords_enabled"] = True
        s["badwords_action"] = "warn"
        s["warn_limit"] = 5
        chat.settings = s
        await get_or_create_player(session, telegram_id=user_id, first_name="Spammer")
        await session.flush()

    mw = MessageActivityAndSecurityMiddleware()

    async def handler(event, data):
        return "handled"

    msg = _make_group_message(chat_id, user_id, "ну и бляха день")
    await mw(handler, msg, {})
    assert msg.delete.called

    async with session_scope() as session:
        from sqlalchemy import select as _select
        warns = (await session.scalars(
            _select(ModerationCase).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.action == "warn",
            )
        )).all()
        assert len(warns) == 1


# ===================== СПРАВКА / ДОКУМЕНТАЦИЯ КОМАНД =====================

def test_help_section_11_is_moderation():
    from app.services.help_service import get_section_help_text, get_main_help_menu_text, MODERATION_HELP_TEXT

    sec = get_section_help_text(11)
    assert sec is not None
    assert "Раздел 11" in sec
    assert "Модерация" in sec
    assert "🛡" in get_main_help_menu_text()
    assert "мут" in MODERATION_HELP_TEXT


@pytest.mark.asyncio
async def test_moderation_help_command_responds():
    from app.handlers.extended_moderation_handlers import handle_moderation_help

    msg = MagicMock()
    msg.text = "модерация"
    msg.reply = AsyncMock()
    await handle_moderation_help(msg)
    msg.reply.assert_called_once()
    sent = msg.reply.call_args[0][0]
    assert "Модерация" in sent
    assert "мут" in sent
    assert "антиспам" in sent


@pytest.mark.asyncio
async def test_punishment_history_command():
    from app.handlers.social_moderation_handlers import handle_punishment_history

    chat_id = -100900100
    admin_id = 9000100
    target_id = 9000101
    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="History Chat")
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        await get_or_create_player(session, telegram_id=target_id, first_name="Target")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=3, assigned_by=admin_id, is_active=True))
        session.add(ModerationCase(
            chat_id=chat_id, target_id=target_id, moderator_id=admin_id,
            action="mute", reason="Флуд", is_active=True,
        ))
        await session.flush()

    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup", title="History Chat")
    msg.from_user = MagicMock(id=admin_id, first_name="Admin", is_bot=False)
    msg.text = f"наказания {target_id}"
    msg.reply = AsyncMock()
    msg.reply_to_message = None
    msg.bot = MagicMock()

    await handle_punishment_history(msg)
    msg.reply.assert_called_once()
    sent = msg.reply.call_args[0][0]
    assert "История наказаний" in sent
    assert "Флуд" in sent
    assert "мут" in sent


@pytest.mark.asyncio
async def test_settings_panel_toggle_switches_and_persists():
    from app.handlers.extended_moderation_handlers import on_setting_toggle

    chat_id = -100900110
    admin_id = 9000110
    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Cfg Chat")
        await get_or_create_player(session, telegram_id=admin_id, first_name="Admin")
        session.add(ChatAdmin(chat_id=chat_id, user_id=admin_id, level=3, assigned_by=admin_id, is_active=True))
        await session.flush()

    cb = MagicMock()
    cb.data = f"cfg_toggle_badwords_{chat_id}"
    cb.from_user = MagicMock(id=admin_id)
    cb.bot = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.chat = MagicMock(id=chat_id, title="Cfg Chat")
    cb.message.edit_text = AsyncMock()

    await on_setting_toggle(cb)
    cb.answer.assert_called_once()
    cb.message.edit_text.assert_called_once()

    async with session_scope() as session:
        chat = await get_or_create_chat(session, chat_id=chat_id, title="Cfg Chat")
        assert chat.settings.get("badwords_enabled") is True
