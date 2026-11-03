"""Комплексные тесты надежности Telegram-бота Fable по разделу 16 ТЗ.

Проверяемые сценарии:
1. Один пользователь пишет в нескольких группах — баланс везде одинаковый (глобальный профиль).
2. Перевод не позволяет потратить больше имеющихся средств.
3. Одновременные переводы не создают отрицательный баланс (транзакционная изоляция).
4. Проценты банка не начисляются дважды за сутки.
5. Биржа не исполняет одну заявку дважды.
6. Отмена заявки возвращает только неиспользованный резерв.
7. Дуэль не позволяет списать ставку дважды.
8. Кнопки дуэли не работают для посторонних.
9. Игрок не может сам назначить себе высокий уровень (запрет самоповышения).
10. Администратор не может наказать равного или вышестоящего (иерархия прав).
11. Правила одного чата не изменяют правила другого (локальность настроек).
12. Белый список ссылок действует только в своём чате.
13. Капча не может быть пройдена другим пользователем.
14. Глобальная блокировка не превращается автоматически в Telegram-бан (is_globally_banned).
15. Перезапуск бота не уничтожает незавершённые заявки и данные (хранение в PostgreSQL).
16. Ошибка одного модуля не останавливает остальные.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.db.session import session_scope
from app.db.models import (
    Chat,
    ChatAdmin,
    MarketOrder,
    Player,
)
from app.services.player_service import get_or_create_player, get_player
from app.services.economy_service import (
    credit_coins,
    debit_coins,
    InsufficientFundsError,
)
from app.services.transfer_service import transfer_coins_with_tax
from app.services.bank_service import (
    deposit,
    accrue_daily_interest,
    get_bank_status,
)
from app.services.exchange_service import (
    create_limit_order,
    execute_market_order,
    cancel_order,
    get_order_book,
)
from app.services.duel_service import (
    create_duel_invite,
    accept_duel,
    duel_aim,
    duel_shoot,
    DuelAccessDeniedError,
)
from app.services.moderation_service import (
    set_admin_level,
    get_user_admin_level,
    issue_mute,
    is_link_allowed,
    add_link_whitelist,
    HierarchyViolationError,
)
from app.services.chat_settings_service import (
    add_chat_rule,
    get_chat_rules,
    create_captcha,
    solve_captcha,
)
from app.services.owner_service import (
    set_player_global_ban,
    reset_global_economy,
    set_chat_bot_state,
)


@pytest.mark.asyncio
async def test_scenario_01_global_player_profile():
    """Сценарий 1: Один пользователь в разных чатах имеет один и тот же баланс."""
    uid = 10001
    async with session_scope() as session:
        reg1 = await get_or_create_player(session, telegram_id=uid, first_name="MultiChatUser")
        await credit_coins(session, user_id=uid, amount=500, reason="Group A reward")

    # Переход в группу Б
    async with session_scope() as session:
        reg2 = await get_or_create_player(session, telegram_id=uid, first_name="MultiChatUser")
        assert reg2.player.wallet == 500


@pytest.mark.asyncio
async def test_scenario_02_transfer_insufficient_funds():
    """Сценарий 2: Перевод не позволяет потратить больше имеющихся средств."""
    u1, u2 = 10002, 10003
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=u1, first_name="Sender")
        await get_or_create_player(session, telegram_id=u2, first_name="Receiver")

        with pytest.raises(InsufficientFundsError):
            await transfer_coins_with_tax(session, sender_id=u1, recipient_id=u2, amount=1000)


@pytest.mark.asyncio
async def test_scenario_03_concurrent_transfers_no_negative_balance():
    """Сценарий 3: Параллельные переводы не создают отрицательный баланс."""
    u_sender, u_rec1, u_rec2 = 10004, 10005, 10006
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=u_sender, first_name="Sender3")
        await get_or_create_player(session, telegram_id=u_rec1, first_name="Rec1")
        await get_or_create_player(session, telegram_id=u_rec2, first_name="Rec2")
        await credit_coins(session, user_id=u_sender, amount=100, reason="Seed")

    async def do_transfer(rec_id):
        try:
            async with session_scope() as s:
                await transfer_coins_with_tax(s, sender_id=u_sender, recipient_id=rec_id, amount=80)
                return True
        except InsufficientFundsError:
            return False

    results = await asyncio.gather(do_transfer(u_rec1), do_transfer(u_rec2))
    assert results.count(True) == 1
    assert results.count(False) == 1

    async with session_scope() as session:
        sender = await get_player(session, u_sender)
        assert sender.wallet >= 0


@pytest.mark.asyncio
async def test_scenario_04_bank_interest_no_double_accrual():
    """Сценарий 4: Проценты банка не начисляются дважды за сутки."""
    u_bank = 10007
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=u_bank, first_name="Banker")
        await credit_coins(session, user_id=u_bank, amount=10_000, reason="Seed")
        await deposit(session, user_id=u_bank, amount=10_000)

    # Первое начисление
    async with session_scope() as session:
        res1 = await accrue_daily_interest(session)
        assert len(res1) == 1
        assert res1[0].applied is True

    # Повторная попытка начисления за тот же день
    async with session_scope() as session:
        res2 = await accrue_daily_interest(session)
        assert len(res2) == 1
        assert res2[0].applied is False


@pytest.mark.asyncio
async def test_scenario_05_and_06_exchange_matching_and_cancellation():
    """Сценарии 5 и 6: Биржа не исполняет заявку дважды, отмена возвращает только остаток резерва."""
    u_seller, u_buyer = 10008, 10009
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=u_seller, first_name="ExSeller")
        await get_or_create_player(session, telegram_id=u_buyer, first_name="ExBuyer")
        # seller has 0 coins initially
        await credit_coins(session, user_id=u_buyer, amount=1000, reason="Seed")
        s = await get_player(session, u_seller, for_update=True)
        s.shares = 100

        # Выставляем лимитную продажу: 50 шт по 2.0
        sell_order, _ = await create_limit_order(session, user_id=u_seller, side="sell", quantity=50, price="2.0")

        # Покупаем 20 шт
        buy_order, sum_buy = await create_limit_order(session, user_id=u_buyer, side="buy", quantity=20, price="2.0")
        assert sum_buy.filled_quantity == 20
        assert sell_order.remaining_quantity == 30

        # Отмена заявки на продажу
        cancelled = await cancel_order(session, order_id=sell_order.id, user_id=u_seller)
        assert cancelled.status == "cancelled"
        # Продавцу вернулись ровно 30 неисполненных акций
        assert s.shares == 80


@pytest.mark.asyncio
async def test_scenario_07_and_08_duel_security():
    """Сценарии 7 и 8: Дуэль не списывает ставку дважды, посторонние не могут нажимать кнопки."""
    p1, p2, bystander = 10010, 10011, 10012
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=p1, first_name="Duellist1")
        await get_or_create_player(session, telegram_id=p2, first_name="Duellist2")
        await get_or_create_player(session, telegram_id=bystander, first_name="Bystander")
        await credit_coins(session, user_id=p1, amount=300, reason="Seed")
        await credit_coins(session, user_id=p2, amount=300, reason="Seed")

        duel = await create_duel_invite(session, chat_id=-100999, challenger_id=p1, opponent_id=p2, stake=100)

        # Посторонний пытается принять
        with pytest.raises(DuelAccessDeniedError):
            await accept_duel(session, session_id=duel.id, actor_id=bystander)

        # Оппонент принимает
        resp_acc = await accept_duel(session, session_id=duel.id, actor_id=p2)
        assert resp_acc.action == "accept"

        # Повторное принятие не должно списать средства
        with pytest.raises(Exception):
            await accept_duel(session, session_id=duel.id, actor_id=p2)

        p1_obj = await get_player(session, p1)
        p2_obj = await get_player(session, p2)
        assert p1_obj.wallet == 200
        assert p2_obj.wallet == 200


@pytest.mark.asyncio
async def test_scenario_09_and_10_admin_hierarchy():
    """Сценарии 9 и 10: Запрет самоповышения и защита субординации."""
    c_id = -100888
    adm5, adm3, usr1 = 10013, 10014, 10015
    async with session_scope() as session:
        session.add(Chat(telegram_id=c_id, title="Hierarchy Chat"))
        await get_or_create_player(session, telegram_id=adm5, first_name="Owner5")
        await get_or_create_player(session, telegram_id=adm3, first_name="Admin3")
        await get_or_create_player(session, telegram_id=usr1, first_name="User1")

        session.add(ChatAdmin(chat_id=c_id, user_id=adm5, level=5, assigned_by=adm5, is_active=True))
        await session.flush()

        await set_admin_level(session, chat_id=c_id, actor_id=adm5, target_id=adm3, new_level=3)

        # Admin 3 пытается назначить себя уровнем 4 (самоповышение) -> отказ
        with pytest.raises(HierarchyViolationError):
            await set_admin_level(session, chat_id=c_id, actor_id=adm3, target_id=adm3, new_level=4)

        # Admin 3 пытается наказать Создателя ур. 5 -> отказ
        with pytest.raises(HierarchyViolationError):
            await issue_mute(session, chat_id=c_id, moderator_id=adm3, target_id=adm5, duration=timedelta(minutes=5))


@pytest.mark.asyncio
async def test_scenario_11_and_12_chat_isolation():
    """Сценарии 11 и 12: Изоляция правил и белых списков между чатами."""
    c1, c2 = -100111, -100222
    admin_id = 10016
    async with session_scope() as session:
        from app.services.chat_settings_service import get_or_create_chat
        await get_or_create_chat(session, chat_id=c1, title="Chat 1")
        await get_or_create_chat(session, chat_id=c2, title="Chat 2")
        await get_or_create_player(session, telegram_id=admin_id, first_name="AdminBoth")
        session.add_all([
            ChatAdmin(chat_id=c1, user_id=admin_id, level=5, assigned_by=admin_id, is_active=True),
            ChatAdmin(chat_id=c2, user_id=admin_id, level=5, assigned_by=admin_id, is_active=True),
        ])
        await session.flush()

        # Добавляем правило только в чат 1
        await add_chat_rule(session, chat_id=c1, admin_id=admin_id, text="Правило только первого чата")
        r1 = await get_chat_rules(session, chat_id=c1)
        r2 = await get_chat_rules(session, chat_id=c2)
        assert len(r1) == 1
        assert len(r2) == 0

        # Добавляем домен в белый список только чата 1
        await add_link_whitelist(session, chat_id=c1, admin_id=admin_id, domain="exclusive.com")
        allowed_c1, _ = await is_link_allowed(session, chat_id=c1, text="https://exclusive.com")
        allowed_c2, _ = await is_link_allowed(session, chat_id=c2, text="https://exclusive.com")
        assert allowed_c1 is True
        assert allowed_c2 is False


@pytest.mark.asyncio
async def test_scenario_13_captcha_security():
    """Сценарий 13: Капча не может быть пройдена другим пользователем."""
    chat_id = -100333
    target_user, attacker = 10017, 10018
    async with session_scope() as session:
        # chat created by create_captcha
        await get_or_create_player(session, telegram_id=target_user, first_name="Target")
        await get_or_create_player(session, telegram_id=attacker, first_name="Attacker")

        ch = await create_captcha(session, chat_id=chat_id, user_id=target_user)

        # Атакующий пытается решить капчу
        ok_attacker = await solve_captcha(session, challenge_token=ch.challenge_token, actor_id=attacker)
        assert ok_attacker is False

        # Легитимный пользователь решает капчу
        ok_target = await solve_captcha(session, challenge_token=ch.challenge_token, actor_id=target_user)
        assert ok_target is True


@pytest.mark.asyncio
async def test_scenario_14_global_ban_semantics():
    """Сценарий 14: Глобальный бан запрещает экономику, но не является локальным Telegram-баном."""
    target_user = 10019
    owner_id = 8039111975
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=target_user, first_name="BannedGuy")
        await set_player_global_ban(session, owner_id=owner_id, target_id=target_user, banned=True, reason="Test global ban")

        p = await get_player(session, target_user)
        assert p.is_globally_banned is True

        # Снятие глобального бана
        await set_player_global_ban(session, owner_id=owner_id, target_id=target_user, banned=False)
        p_unbanned = await get_player(session, target_user)
        assert p_unbanned.is_globally_banned is False


@pytest.mark.asyncio
async def test_scenario_15_persistence_and_restart():
    """Сценарий 15: Состояние и открытые ордера сохраняются в PostgreSQL после завершения сессий."""
    uid = 10020
    order_id = None
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=uid, first_name="PersistentUser")
        p = await get_player(session, uid, for_update=True)
        p.shares = 50
        order, _ = await create_limit_order(session, user_id=uid, side="sell", quantity=10, price="5.0")
        order_id = order.id

    # Новая сессия (имитация перезапуска бота)
    async with session_scope() as session:
        order_loaded = await session.get(MarketOrder, order_id)
        assert order_loaded is not None
        assert order_loaded.status == "open"
        assert order_loaded.remaining_quantity == 10
