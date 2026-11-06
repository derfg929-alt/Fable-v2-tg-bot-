import pytest
from unittest.mock import AsyncMock, MagicMock
from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.chat_settings_service import get_or_create_chat
from app.services.games_service import (
    play_slots,
    play_dice,
    play_coinflip,
    play_mystery_box,
    play_fishing,
    play_hunting,
    FISHING_LOOT,
    HUNTING_LOOT,
)
from app.services.owner_service import (
    list_players_wealth_summary,
    get_player_wealth_details,
)
from app.handlers.owner_handlers import (
    handle_owner_players_balances,
    handle_owner_player_info,
    on_owner_menu_balances,
)
from app.handlers.social_moderation_handlers import (
    handle_chat_purge,
    handle_view_warns,
    handle_pin_message,
    handle_unpin_message,
)
from app.handlers.extended_moderation_handlers import (
    handle_ban,
    handle_kick,
    handle_admin_appoint,
)


@pytest.mark.asyncio
async def test_gambling_odds_and_slots_text_reels():
    """Проверка повышенных коэффициентов и генерации текстовых слотов без dice."""
    uid = 11223344
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="Gambler")
        p = reg.player
        p.wallet = 500_000
        await session.flush()

        # Slots without dice_value
        res_slots = await play_slots(session, user_id=uid, stake=100)
        assert len(res_slots.reels) == 3
        assert res_slots.multiplier >= 0.0

        # Dice win payout is 2.0x
        for _ in range(5):
            p.wallet = 500_000
            await session.flush()
            p_roll, b_roll, is_win, payout = await play_dice(session, user_id=uid, stake=100)
            if is_win:
                assert payout == 200

        # Mystery box
        p.wallet = 500_000
        await session.flush()
        mult, payout = await play_mystery_box(session, user_id=uid, stake=100)
        assert mult in [0.0, 0.5, 1.2, 1.5, 2.0, 3.0, 5.0]


@pytest.mark.asyncio
async def test_loot_chances_nerfed():
    """Проверка, что шансы на эпический и легендарный дроп снижены."""
    gold_fish = [item for item in FISHING_LOOT if item[0] == "epic"][0]
    chest = [item for item in FISHING_LOOT if item[0] == "legendary"][0]
    assert gold_fish[3] <= 0.03
    assert chest[3] <= 0.01

    bear = [item for item in HUNTING_LOOT if item[0] == "epic"][0]
    dragon = [item for item in HUNTING_LOOT if item[0] == "legendary"][0]
    assert bear[3] <= 0.03
    assert dragon[3] <= 0.01


@pytest.mark.asyncio
async def test_owner_wealth_inspection():
    """Проверка просмотра балансов игроков владельцем."""
    owner_id = 8039111975
    target_id = 99881122

    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=target_id, first_name="RichGuy")
        player = reg.player
        player.wallet = 1_500_000
        player.bank_balance = 500_000
        await session.flush()

        # 1. Summary
        summary = await list_players_wealth_summary(session, owner_id=owner_id, limit=10)
        assert any(pl.telegram_id == target_id for pl in summary)

        # 2. Details
        details = await get_player_wealth_details(session, owner_id=owner_id, target_id=target_id)
        assert details is not None
        assert details.wallet == 1_500_000
        assert details.bank_balance == 500_000

    # 3. Callback button
    cb = MagicMock()
    cb.from_user = MagicMock(id=owner_id)
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    await on_owner_menu_balances(cb)
    cb.answer.assert_called_once()
    assert cb.message.edit_text.called


@pytest.mark.asyncio
async def test_moderation_synonyms_and_iris_features():
    """Проверка команд модерации в стиле Ирис."""
    chat_id = -10077889900
    admin_id = 8039111975  # level 5
    target_id = 44556677

    mock_bot = MagicMock()
    mock_bot.ban_chat_member = AsyncMock()
    mock_bot.unban_chat_member = AsyncMock()
    mock_bot.delete_message = AsyncMock()
    mock_bot.pin_chat_message = AsyncMock()
    mock_bot.unpin_chat_message = AsyncMock()

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Test Group")
        await get_or_create_player(session, telegram_id=admin_id, first_name="OwnerAdmin")
        await get_or_create_player(session, telegram_id=target_id, first_name="TargetUser")

    # 1. Бан с синонимом "!бан"
    msg_ban = MagicMock()
    msg_ban.chat = MagicMock(id=chat_id, type="supergroup")
    msg_ban.from_user = MagicMock(id=admin_id)
    msg_ban.text = f"!бан {target_id} Спам"
    msg_ban.reply_to_message = None
    msg_ban.entities = None
    msg_ban.reply = AsyncMock()

    await handle_ban(msg_ban, bot=mock_bot)
    assert msg_ban.reply.called
    assert "ЗАБЛОКИРОВАН" in msg_ban.reply.call_args[0][0]

    # 2. Кик с синонимом "кикнуть"
    msg_kick = MagicMock()
    msg_kick.chat = MagicMock(id=chat_id, type="supergroup")
    msg_kick.from_user = MagicMock(id=admin_id)
    msg_kick.text = f"кикнуть {target_id} Флуд"
    msg_kick.reply_to_message = None
    msg_kick.entities = None
    msg_kick.reply = AsyncMock()

    await handle_kick(msg_kick, bot=mock_bot)
    assert msg_kick.reply.called
    assert "ИСКЛЮЧЕН" in msg_kick.reply.call_args[0][0]

    # 3. Чистка
    msg_purge = MagicMock()
    msg_purge.chat = MagicMock(id=chat_id, type="supergroup")
    msg_purge.from_user = MagicMock(id=admin_id)
    msg_purge.text = "чистка 5"
    msg_purge.message_id = 100
    msg_purge.bot = mock_bot
    msg_purge.delete = AsyncMock()
    msg_purge.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))

    await handle_chat_purge(msg_purge)
    assert mock_bot.delete_message.called
