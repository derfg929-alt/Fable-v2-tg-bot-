"""Тесты доработанного инвентаря (поштучная и полная продажа) и модерации (мут, размут, бан, кик)."""
from __future__ import annotations

import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from app.db.session import session_scope
from app.db.models import Player, PlayerMine, PlayerInventory, ModerationCase, ChatAdmin
from app.services.player_service import get_or_create_player
from app.services.mine_service import get_or_create_mine, ORE_PRICES
from app.services.inventory_service import (
    get_full_player_inventory,
    sell_inventory_item,
    sell_all_inventory,
    resolve_item_key,
)
from app.services.moderation_service import (
    get_user_admin_level,
    issue_mute,
    revoke_mute,
    record_ban_or_kick,
    check_can_moderate,
    PermissionDeniedError,
    HierarchyViolationError,
)
from app.handlers.economy_handlers import handle_inventory, handle_inventory_sell_cmd
from app.handlers.social_moderation_handlers import handle_mute, handle_unmute
from app.handlers.extended_moderation_handlers import handle_ban, handle_kick, handle_unban


@pytest.mark.asyncio
async def test_resolve_item_key():
    assert resolve_item_key("уголь") == "coal"
    assert resolve_item_key("угля") == "coal"
    assert resolve_item_key("железо") == "iron"
    assert resolve_item_key("слитки") == "processed_metals"
    assert resolve_item_key("заяц") == "заяц"
    assert resolve_item_key("зайцев") == "заяц"
    assert resolve_item_key("щука") == "щука"
    assert resolve_item_key("золотая рыбка") == "золотая рыбка"


@pytest.mark.asyncio
async def test_sell_piece_by_piece_and_full():
    uid = 998811
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=uid, first_name="Trader")
        mine = await get_or_create_mine(session, user_id=uid)
        mine.coal = 10
        mine.iron = 5
        mine.processed_metals = 2

        # Добавляем улов и охоту
        fish = PlayerInventory(user_id=uid, item_id="щука", item_name="Щука", category="fishing", quantity=3)
        hunt = PlayerInventory(user_id=uid, item_id="заяц", item_name="Заяц", category="hunting", quantity=4)
        session.add_all([fish, hunt])
        await session.flush()

        # 1. Проверяем отображение полного инвентаря
        inv_data = await get_full_player_inventory(session, user_id=uid)
        assert len(inv_data["mine_items"]) == 3
        assert len(inv_data["farm_loot"]) == 2
        assert inv_data["total_value"] > 0

        # 2. Продажа поштучно: 2 зайца
        res_hunt = await sell_inventory_item(session, user_id=uid, item_query="заяц", quantity=2)
        assert res_hunt.quantity == 2
        assert res_hunt.remaining_quantity == 2
        assert res_hunt.total_earned == 2 * 75

        # 3. Продажа поштучно: 4 угля
        res_coal = await sell_inventory_item(session, user_id=uid, item_query="уголь", quantity=4)
        assert res_coal.quantity == 4
        assert res_coal.remaining_quantity == 6
        assert res_coal.total_earned == 4 * ORE_PRICES["coal"]

        # 4. Продажа всей оставшейся добычи
        res_all = await sell_all_inventory(session, user_id=uid)
        assert res_all.total_earned > 0
        assert res_all.sold_items_count > 0

        # Проверяем, что запасы обнулились
        inv_data_after = await get_full_player_inventory(session, user_id=uid)
        assert len(inv_data_after["mine_items"]) == 0
        assert len(inv_data_after["farm_loot"]) == 0
        assert inv_data_after["total_value"] == 0


@pytest.mark.asyncio
async def test_telegram_moderation_sync_and_permissions():
    chat_id = -100999888777
    creator_id = 998821
    moderator_id = 998822
    regular_user_id = 998823

    mock_bot = MagicMock()
    # Creator member
    creator_member = MagicMock()
    creator_member.status = "creator"
    # Admin member
    admin_member = MagicMock()
    admin_member.status = "administrator"
    # Regular member
    reg_member = MagicMock()
    reg_member.status = "member"

    async def mock_get_chat_member(chat_id, user_id):
        if user_id == creator_id:
            return creator_member
        elif user_id == moderator_id:
            return admin_member
        return reg_member

    mock_bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member)
    mock_bot.restrict_chat_member = AsyncMock(return_value=True)
    mock_bot.ban_chat_member = AsyncMock(return_value=True)
    mock_bot.unban_chat_member = AsyncMock(return_value=True)

    from app.services.chat_settings_service import get_or_create_chat

    async with session_scope() as session:
        await get_or_create_chat(session, chat_id=chat_id, title="Test Group")
        await get_or_create_player(session, telegram_id=creator_id, first_name="Creator")
        await get_or_create_player(session, telegram_id=moderator_id, first_name="Mod")
        await get_or_create_player(session, telegram_id=regular_user_id, first_name="User")
        await session.flush()

        # Проверяем авто-синхронизацию уровня прав создателя: ур. 5
        lvl_creator = await get_user_admin_level(session, chat_id=chat_id, user_id=creator_id, bot=mock_bot)
        assert lvl_creator == 5

        # Администраторы Telegram не получают права автоматически (ур. 1)
        lvl_mod = await get_user_admin_level(session, chat_id=chat_id, user_id=moderator_id, bot=mock_bot)
        assert lvl_mod == 1

        lvl_user = await get_user_admin_level(session, chat_id=chat_id, user_id=regular_user_id, bot=mock_bot)
        assert lvl_user == 1

        # Создатель назначает модератора на ур. 3
        from app.services.moderation_service import set_admin_level
        await set_admin_level(session, chat_id=chat_id, actor_id=creator_id, target_id=moderator_id, new_level=3, bot=mock_bot)
        lvl_mod_after = await get_user_admin_level(session, chat_id=chat_id, user_id=moderator_id, bot=mock_bot)
        assert lvl_mod_after == 3

        # Мут: назначенный админ (ур 3) мутит обычного пользователя (ур 1)
        mute_case = await issue_mute(
            session,
            chat_id=chat_id,
            moderator_id=moderator_id,
            target_id=regular_user_id,
            duration=timedelta(minutes=30),
            reason="Спам",
            bot=mock_bot,
        )
        assert mute_case.is_active is True
        assert mute_case.action == "mute"

        # Размут
        await revoke_mute(session, chat_id=chat_id, moderator_id=moderator_id, target_id=regular_user_id, bot=mock_bot)

        # Бан и кик
        ban_case = await record_ban_or_kick(
            session,
            chat_id=chat_id,
            moderator_id=moderator_id,
            target_id=regular_user_id,
            action="ban",
            reason="Реклама",
            bot=mock_bot,
        )
        assert ban_case.action == "ban"

        # Попытка обычного юзера наказать админа должна вызвать ошибку прав
        with pytest.raises(PermissionDeniedError):
            await check_can_moderate(session, chat_id=chat_id, moderator_id=regular_user_id, target_id=moderator_id, min_required_level=2, bot=mock_bot)

        # Попытка админа ур 3 наказать создателя ур 5 должна вызвать ошибку субординации
        with pytest.raises(HierarchyViolationError):
            await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=creator_id, min_required_level=2, bot=mock_bot)
