import pytest
from unittest.mock import AsyncMock, MagicMock
from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.chat_settings_service import get_or_create_chat
from app.services.games_service import (
    play_slots,
    decode_telegram_slot_dice,
    TELEGRAM_SLOT_SYMBOLS,
)
from app.services.moderation_service import (
    get_user_admin_level,
    set_admin_level,
    ROLE_NAMES,
)
from app.handlers.social_moderation_handlers import (
    on_help_section_click,
    on_top_menu_rich,
    on_top_menu_back,
    on_clan_delete_cancel,
)
from app.handlers.owner_handlers import (
    on_owner_menu_back,
    on_owner_menu_chats,
    on_owner_menu_stats,
    on_owner_eco_reset_prompt,
    on_reset_economy_cancel,
)
from app.handlers.extended_moderation_handlers import (
    handle_admin_dismiss,
    on_preset_select,
)


def test_telegram_slot_dice_decoding():
    """Проверяем соответствие кодировки слотов Telegram 1..64."""
    # Value 1 -> BAR BAR BAR
    assert decode_telegram_slot_dice(1) == ("BAR", "BAR", "BAR")
    # Value 22 -> 🍇 🍇 🍇
    assert decode_telegram_slot_dice(22) == ("🍇", "🍇", "🍇")
    # Value 43 -> 🍋 🍋 🍋
    assert decode_telegram_slot_dice(43) == ("🍋", "🍋", "🍋")
    # Value 64 -> 7️⃣ 7️⃣ 7️⃣ (Джекпот)
    assert decode_telegram_slot_dice(64) == ("7️⃣", "7️⃣", "7️⃣")

    # Проверяем все 64 значения
    for v in range(1, 65):
        reels = decode_telegram_slot_dice(v)
        assert len(reels) == 3
        for r in reels:
            assert r in TELEGRAM_SLOT_SYMBOLS


@pytest.mark.asyncio
async def test_slots_outcomes_and_payouts():
    """Тест выигрышей слотов при конкретных значениях dice."""
    uid = 99887711
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="SlotPlayer")
        p = reg.player
        p.wallet = 100_000
        await session.flush()

        # Джекпот: value 64 -> 777 -> x15
        res_jackpot = await play_slots(session, user_id=uid, stake=100, dice_value=64)
        assert res_jackpot.is_win is True
        assert res_jackpot.multiplier == 15.0
        assert res_jackpot.payout == 1500
        assert res_jackpot.reels == ("7️⃣", "7️⃣", "7️⃣")

        # Три лимона: value 43 -> x5
        res_lemon = await play_slots(session, user_id=uid, stake=100, dice_value=43)
        assert res_lemon.is_win is True
        assert res_lemon.multiplier == 5.0
        assert res_lemon.payout == 500

        # Три винограда: value 22 -> x5
        res_grape = await play_slots(session, user_id=uid, stake=100, dice_value=22)
        assert res_grape.is_win is True
        assert res_grape.multiplier == 5.0
        assert res_grape.payout == 500

        # Три BAR: value 1 -> x5
        res_bar = await play_slots(session, user_id=uid, stake=100, dice_value=1)
        assert res_bar.is_win is True
        assert res_bar.multiplier == 5.0
        assert res_bar.payout == 500

        # Две семерки (например value 16: idx 15 -> (3, 3, 0) -> 7, 7, BAR) -> x1.2
        res_pair7 = await play_slots(session, user_id=uid, stake=100, dice_value=16)
        assert res_pair7.is_win is True
        assert res_pair7.multiplier == 1.2
        assert res_pair7.payout == 120

        # Все разные (например value 28: idx 27 -> (3, 2, 1) -> 7, 🍋, 🍇) -> LOSE
        res_diff = await play_slots(session, user_id=uid, stake=100, dice_value=28)
        assert res_diff.is_win is False
        assert res_diff.multiplier == 0.0
        assert res_diff.payout == 0


@pytest.mark.asyncio
async def test_button_callbacks_always_answered():
    """Тест: нажатие на инлайн кнопки вызывает callback.answer() без задержек."""
    # 1. help_sec_1
    cb = MagicMock()
    cb.data = "help_sec_1"
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    await on_help_section_click(cb)
    cb.answer.assert_called_once()
    cb.message.edit_text.assert_called_once()

    # 2. clan_del_cancel
    cb_clan = MagicMock()
    cb_clan.data = "clan_del_cancel"
    cb_clan.answer = AsyncMock()
    cb_clan.message = MagicMock()
    cb_clan.message.edit_text = AsyncMock()
    await on_clan_delete_cancel(cb_clan)
    cb_clan.answer.assert_called_once()

    # 3. top_menu_back
    cb_top = MagicMock()
    cb_top.data = "top_menu_back"
    cb_top.answer = AsyncMock()
    cb_top.message = MagicMock()
    cb_top.message.edit_text = AsyncMock()
    await on_top_menu_back(cb_top)
    cb_top.answer.assert_called_once()

    # 4. owner_menu_stats
    cb_owner = MagicMock()
    cb_owner.data = "owner_menu_stats"
    cb_owner.from_user = MagicMock(id=8039111975)
    cb_owner.answer = AsyncMock()
    cb_owner.message = MagicMock()
    cb_owner.message.edit_text = AsyncMock()
    await on_owner_menu_stats(cb_owner)
    cb_owner.answer.assert_called_once()

    # 5. preset_select
    cb_preset = MagicMock()
    cb_preset.data = "preset_strict_-100999888"
    cb_preset.from_user = MagicMock(id=8039111975)
    cb_preset.answer = AsyncMock()
    cb_preset.message = MagicMock()
    cb_preset.message.edit_text = AsyncMock()
    await on_preset_select(cb_preset)
    cb_preset.answer.assert_called_once()


@pytest.mark.asyncio
async def test_admin_sync_and_dismiss_command():
    """Тест автоматической синхронизации прав администраторов и команды снятия админки."""
    chat_id = -100554433221
    creator_id = 99887722
    target_id = 99887733

    mock_bot = MagicMock()
    # Эмулируем статус creator в Telegram
    creator_member = MagicMock()
    creator_member.status = "creator"
    mock_bot.get_chat_member = AsyncMock(return_value=creator_member)

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="TestGroup")
        await get_or_create_player(session, telegram_id=creator_id, first_name="ChatCreator")
        await get_or_create_player(session, telegram_id=target_id, first_name="TargetUser")

        # Проверяем синхронизацию
        lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=creator_id, bot=mock_bot)
        assert lvl == 5

        # Назначаем target_id модератором (уровень 2)
        new_lvl = await set_admin_level(session, chat_id=chat_id, actor_id=creator_id, target_id=target_id, new_level=2)
        assert new_lvl == 2

        # Проверяем уровень цели
        target_lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=target_id)
        assert target_lvl == 2

    # Тестируем команду 'админ снять' через handler
    msg = MagicMock()
    msg.chat = MagicMock(id=chat_id, type="supergroup")
    msg.from_user = MagicMock(id=creator_id)
    msg.text = f"админ снять {target_id}"
    msg.reply_to_message = None
    msg.entities = None
    msg.reply = AsyncMock()

    await handle_admin_dismiss(msg, bot=mock_bot)
    assert msg.reply.called
    reply_text = msg.reply.call_args[0][0]
    assert "сняты права администратора" in reply_text

    # Проверяем, что в базе уровень сброшен
    async with session_scope() as session:
        revoked_lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=target_id)
        assert revoked_lvl == 1
