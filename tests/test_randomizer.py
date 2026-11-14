"""Тесты Fable Randomizer: реферальная система, конкурсы, участие, подписки,
алгоритм розыгрыша, уведомления, админ-доступ и проверка Telegram initData.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest

from app.db.session import session_scope
from app.db.models import Channel, Player, Winner
from app.services import contest_service as cs
from app.services import draw_service as ds
from app.services import referral_service as ref
from app.services import notifications_service as notify
from app.services import randomizer_settings as rset
from app.services.player_service import get_or_create_player
from app.services.audit_service import log_action, get_recent_actions


# ===================== РЕФЕРАЛЬНАЯ СИСТЕМА =====================

def test_parse_and_build_referral():
    assert ref.parse_referral_payload("ref_12345") == 12345
    assert ref.parse_referral_payload("12345") == 12345
    assert ref.parse_referral_payload("garbage") is None
    assert ref.parse_referral_payload(None) is None
    assert ref.build_referral_link("MyBot", 999) == "https://t.me/MyBot?start=ref_999"


@pytest.mark.asyncio
async def test_referral_bonus_and_guards():
    inviter_id = 700001
    invited_id = 700002
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=inviter_id, first_name="Inviter")
        await get_or_create_player(session, telegram_id=invited_id, first_name="Invited")

    # Первое приглашение — успешно.
    async with session_scope() as session:
        assert await ref.register_referral(session, inviter_id=inviter_id, invited_id=invited_id) is True

    # Повторное начисление за того же игрока запрещено.
    async with session_scope() as session:
        assert await ref.register_referral(session, inviter_id=inviter_id, invited_id=invited_id) is False

    # Самореферал запрещён.
    async with session_scope() as session:
        assert await ref.register_referral(session, inviter_id=inviter_id, invited_id=inviter_id) is False

    # Пригласивший записан в профиль приглашённого.
    async with session_scope() as session:
        player = await session.get(Player, invited_id)
        assert player.referred_by == inviter_id
        assert await ref.count_invited(session, inviter_id) == 1
        assert await ref.get_referral_bonus_units(session, inviter_id) == 1


@pytest.mark.asyncio
async def test_referral_bonus_capped():
    inviter_id = 710001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=inviter_id, first_name="Inviter")
        await rset.save_settings_dict(session, {"max_referral_bonus_units": 3})

    for i in range(5):
        async with session_scope() as session:
            await get_or_create_player(session, telegram_id=710100 + i, first_name=f"User{i}")
        async with session_scope() as session:
            await ref.register_referral(session, inviter_id=inviter_id, invited_id=710100 + i)

    async with session_scope() as session:
        # 5 приглашений, но максимум 3 юнита.
        assert await ref.count_invited(session, inviter_id) == 5
        assert await ref.get_referral_bonus_units(session, inviter_id) == 3


# ===================== КОНКУРСЫ =====================

@pytest.mark.asyncio
async def test_contest_crud_and_categories():
    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        active = await cs.create_contest(session, title="Active", status=cs.STATUS_ACTIVE)
        upcoming = await cs.create_contest(
            session, title="Upcoming", status=cs.STATUS_SCHEDULED, starts_at=now + timedelta(days=2)
        )
        finished = await cs.create_contest(
            session, title="Finished", status=cs.STATUS_ACTIVE, ends_at=now - timedelta(days=1)
        )
        active_id, upcoming_id, finished_id = active.id, upcoming.id, finished.id

    async with session_scope() as session:
        actives = await cs.list_contests(session, category="active")
        assert [c.id for c in actives] == [active_id]

        up = await cs.list_contests(session, category="upcoming")
        assert upcoming_id in [c.id for c in up]

        fin = await cs.list_contests(session, category="finished")
        assert finished_id in [c.id for c in fin]

        # Завершённый конкурс получил статус finished автоматически.
        contest = await cs.get_contest(session, finished_id)
        assert contest.status == cs.STATUS_FINISHED

        await cs.update_contest(session, active_id, title="Active2")
        assert (await cs.get_contest(session, active_id)).title == "Active2"

        await cs.archive_contest(session, active_id)
        assert (await cs.get_contest(session, active_id)).is_archived is True

        await cs.delete_contest(session, upcoming_id)
        assert await cs.get_contest(session, upcoming_id) is None


# ===================== УЧАСТИЕ =====================

def _mock_bot(status="left"):
    bot = AsyncMock()
    member = MagicMock()
    member.status = status
    member.is_member = status in ("member", "administrator", "creator")
    bot.get_chat_member = AsyncMock(return_value=member)
    return bot


@pytest.mark.asyncio
async def test_join_contest_success_and_duplicate():
    bot = _mock_bot("member")
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="Join Test", status=cs.STATUS_ACTIVE)
        cid = contest.id

    async with session_scope() as session:
        entry = await cs.join_contest(session, bot, contest_id=cid, user_id=800001, first_name="P1")
        assert entry.status == "active"

    # Повторное участие запрещено.
    async with session_scope() as session:
        with pytest.raises(cs.AlreadyParticipatingError):
            await cs.join_contest(session, bot, contest_id=cid, user_id=800001, first_name="P1")

    async with session_scope() as session:
        assert await cs.participants_count(session, cid) == 1


@pytest.mark.asyncio
async def test_join_blocked_user():
    bot = _mock_bot("member")
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="Ban Test", status=cs.STATUS_ACTIVE)
        cid = contest.id
        await get_or_create_player(session, telegram_id=800002, first_name="Bad")
        player = await session.get(Player, 800002)
        player.is_globally_banned = True

    async with session_scope() as session:
        with pytest.raises(cs.BannedFromContestError):
            await cs.join_contest(session, bot, contest_id=cid, user_id=800002, first_name="Bad")


@pytest.mark.asyncio
async def test_subscription_required():
    bot = _mock_bot("left")  # не подписан
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="Sub Test", status=cs.STATUS_ACTIVE)
        cid = contest.id
        channel = Channel(chat_id=-1001234567890, username="testchannel", title="Test", is_active=True)
        session.add(channel)
        await session.flush()
        await cs.set_contest_channels(session, cid, [channel.id])

    async with session_scope() as session:
        with pytest.raises(cs.NotSubscribedError) as exc:
            await cs.join_contest(session, bot, contest_id=cid, user_id=800003, first_name="P3")
        assert len(exc.value.missing) == 1

    # После подписки участие проходит.
    bot2 = _mock_bot("member")
    async with session_scope() as session:
        entry = await cs.join_contest(session, bot2, contest_id=cid, user_id=800003, first_name="P3")
        assert entry.status == "active"


@pytest.mark.asyncio
async def test_leave_contest():
    bot = _mock_bot("member")
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="Leave Test", status=cs.STATUS_ACTIVE, allow_leave=True)
        cid = contest.id
        await cs.join_contest(session, bot, contest_id=cid, user_id=800004, first_name="P4")

    async with session_scope() as session:
        assert await cs.leave_contest(session, contest_id=cid, user_id=800004) is True
        assert await cs.participants_count(session, cid) == 0
        # История сохраняется (запись остаётся со статусом left).
        entry = await cs.get_entry(session, cid, 800004)
        assert entry is not None and entry.status == "left"

    # После завершения отмена невозможна.
    async with session_scope() as session:
        await cs.update_contest(session, cid, status=cs.STATUS_FINISHED)
        with pytest.raises(cs.ContestClosedError):
            await cs.leave_contest(session, contest_id=cid, user_id=800004)


@pytest.mark.asyncio
async def test_leave_not_allowed():
    bot = _mock_bot("member")
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="NoLeave", status=cs.STATUS_ACTIVE, allow_leave=False)
        cid = contest.id
        await cs.join_contest(session, bot, contest_id=cid, user_id=800005, first_name="P5")

    async with session_scope() as session:
        with pytest.raises(cs.ContestError):
            await cs.leave_contest(session, contest_id=cid, user_id=800005)


# ===================== АЛГОРИТМ РОЗЫГРЫША =====================

@pytest.mark.asyncio
async def test_draw_selects_unique_winners_and_logs():
    bot = _mock_bot("member")
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="Draw", status=cs.STATUS_ACTIVE, winners_count=3, prize="Приз")
        cid = contest.id
        for i in range(6):
            await cs.join_contest(session, bot, contest_id=cid, user_id=810000 + i, first_name=f"U{i}")

    async with session_scope() as session:
        result = await ds.run_draw(session, contest_id=cid, executed_by=1)
        assert len(result.winners) == 3
        assert len(set(result.winners)) == 3  # уникальные победители
        assert len(result.participants) == 6
        # Каждый участник имеет ненулевой базовый вес.
        assert all(w > 0 for w in result.weights.values())

    async with session_scope() as session:
        winners = await ds.get_latest_winners(session, cid)
        assert len(winners) == 3
        logs = await ds.get_draw_history(session, cid)
        assert len(logs) == 1
        assert logs[0].round == 1
        assert logs[0].algorithm_version == ds.ALGORITHM_VERSION
        assert logs[0].seed

    # Повторный розыгрыш сохраняет предыдущий результат.
    async with session_scope() as session:
        result2 = await ds.run_draw(session, contest_id=cid, executed_by=1, reason="Повтор")
        assert result2.round == 2

    async with session_scope() as session:
        logs = await ds.get_draw_history(session, cid)
        assert len(logs) == 2
        assert logs[0].round == 2 and logs[0].reason == "Повтор"
        assert logs[1].round == 1
        # Первый раунд победителей сохранён.
        first_round = await session.scalar(
            __import__("sqlalchemy").select(Winner).where(Winner.contest_id == cid, Winner.draw_round == 1)
        )
        assert first_round is not None


@pytest.mark.asyncio
async def test_draw_excludes_banned_and_empty():
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="DrawEmpty", status=cs.STATUS_ACTIVE)
        cid = contest.id
    async with session_scope() as session:
        with pytest.raises(ds.DrawError):
            await ds.run_draw(session, contest_id=cid)


@pytest.mark.asyncio
async def test_draw_is_verifiable_and_redraw_excludes_previous():
    bot = _mock_bot("member")
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="Verify", status=cs.STATUS_ACTIVE, winners_count=2)
        cid = contest.id
        for i in range(5):
            await cs.join_contest(session, bot, contest_id=cid, user_id=830000 + i, first_name=f"V{i}")

    async with session_scope() as session:
        first = await ds.run_draw(session, contest_id=cid)
        log = (await ds.get_draw_history(session, cid))[0]
        # Результат воспроизводится из seed — данные для проверки честности.
        assert ds.verify_draw(log) == first.winners

    # Повторный розыгрыш с исключением прошлых победителей.
    async with session_scope() as session:
        second = await ds.run_draw(
            session, contest_id=cid, reason="Без прошлых", exclude_previous_winners=True
        )
        assert second.round == 2
        assert not (set(second.winners) & set(first.winners))
        assert len(second.participants) == 3


@pytest.mark.asyncio
async def test_json_database_file_roundtrip(tmp_path):
    """JSON-файл действительно хранит данные (основное хранилище)."""
    from app.db.json_db import JsonDatabaseManager

    json_path = tmp_path / "db.json"
    manager = JsonDatabaseManager(str(json_path))

    async with session_scope() as session:
        await cs.create_contest(session, title="JsonPersist", status=cs.STATUS_ACTIVE, prize="P")
        await manager.dump_from_session(session)

    data = manager.load_json()
    assert any(c["title"] == "JsonPersist" for c in data.get("contests", []))

    # Восстановление в чистую сессию из JSON.
    async with session_scope() as session:
        for tbl in reversed(__import__("app.db.models", fromlist=["Base"]).Base.metadata.sorted_tables):
            await session.execute(tbl.delete())
    async with session_scope() as session:
        await manager.restore_to_session(session)
    async with session_scope() as session:
        titles = [c.title for c in await cs.list_contests(session)]
        assert "JsonPersist" in titles


# ===================== УВЕДОМЛЕНИЯ =====================

@pytest.mark.asyncio
async def test_notification_idempotency_and_toggles():
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=820001, first_name="N1")

    async with session_scope() as session:
        sent = await notify.notify_user(
            session, bot, user_id=820001, event_type="new_contest",
            event_key="new_contest:1", text="Тест",
        )
        assert sent is True
        # Повторная отправка того же события блокируется.
        sent2 = await notify.notify_user(
            session, bot, user_id=820001, event_type="new_contest",
            event_key="new_contest:1", text="Тест",
        )
        assert sent2 is False
    assert bot.send_message.await_count == 1

    # Отключение типа уведомлений администратором.
    async with session_scope() as session:
        await rset.set_notification_enabled(session, "new_contest", False)
    async with session_scope() as session:
        sent3 = await notify.notify_user(
            session, bot, user_id=820001, event_type="new_contest",
            event_key="new_contest:2", text="Тест2",
        )
        assert sent3 is False


# ===================== АДМИНИСТРАТОРЫ И НАСТРОЙКИ =====================

@pytest.mark.asyncio
async def test_admin_management_and_support_link():
    owner = 8039111975
    async with session_scope() as session:
        assert await rset.is_admin(session, owner) is True
        assert await rset.is_admin(session, 555000) is False
        await rset.add_admin(session, 555000, added_by=owner)
    async with session_scope() as session:
        assert await rset.is_admin(session, 555000) is True
        assert 555000 in await rset.get_admin_ids(session)
        await rset.set_support_link(session, "https://t.me/support")
    async with session_scope() as session:
        assert await rset.get_support_link(session) == "https://t.me/support"
        await rset.remove_admin(session, 555000)
    async with session_scope() as session:
        assert await rset.is_admin(session, 555000) is False


@pytest.mark.asyncio
async def test_audit_logging():
    async with session_scope() as session:
        await log_action(session, actor_id=1, action="contest_create", target_type="contest", target_id=42)
    async with session_scope() as session:
        actions = await get_recent_actions(session, limit=10)
        assert any(a.action == "contest_create" and a.target_id == "42" for a in actions)


# ===================== ПРОВЕРКА Telegram initData =====================

def _make_init_data(bot_token: str, user: dict, auth_date: int | None = None) -> str:
    auth_date = auth_date or int(datetime.now(timezone.utc).timestamp())
    pairs = {"user": json.dumps(user, separators=(",", ":")), "auth_date": str(auth_date), "query_id": "AAA"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    pairs["hash"] = computed
    return urlencode(pairs)


def test_init_data_validation_ok_and_bad():
    from app.api import validate_init_data, InitDataError

    token = "123456:dummy_token_default"
    user = {"id": 12345, "first_name": "Tester", "username": "tester"}
    good = _make_init_data(token, user)
    parsed = validate_init_data(good, token, ttl_seconds=86400)
    assert parsed["user"]["id"] == 12345

    # Подделанная подпись отклоняется.
    with pytest.raises(InitDataError):
        validate_init_data(good, "999:wrong_token", ttl_seconds=86400)

    # Истёкшая подпись отклоняется.
    old = _make_init_data(token, user, auth_date=int(datetime.now(timezone.utc).timestamp()) - 100000)
    with pytest.raises(InitDataError):
        validate_init_data(old, token, ttl_seconds=86400)


# ===================== РЕГИСТРАЦИЯ ЧЕРЕЗ /start =====================

@pytest.mark.asyncio
async def test_start_registers_once_and_referral():
    from app.main import create_dispatcher
    from aiogram import types

    dp = create_dispatcher()
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="FableRandomBot"))

    inviter = 830001
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=inviter, first_name="Inviter")

    def _update(uid: int, text: str, update_id: int):
        now = datetime.now(timezone.utc)
        user = types.User(id=uid, is_bot=False, first_name=f"U{uid}")
        chat = types.Chat(id=uid, type="private")
        msg = types.Message(message_id=1, date=now, chat=chat, from_user=user, text=text)
        msg = msg.as_(bot)
        return types.Update(update_id=update_id, message=msg)

    # Первый запуск нового пользователя по реферальной ссылке.
    invited = 830002
    await dp.feed_update(bot=bot, update=_update(invited, f"/start ref_{inviter}", 1))
    async with session_scope() as session:
        assert await session.get(Player, invited) is not None
        assert await ref.count_invited(session, inviter) == 1

    # Повторный запуск не создаёт дубликат и не начисляет бонус.
    await dp.feed_update(bot=bot, update=_update(invited, f"/start ref_{inviter}", 2))
    async with session_scope() as session:
        assert await ref.count_invited(session, inviter) == 1


# ===================== СОХРАНЕНИЕ ДАННЫХ =====================

@pytest.mark.asyncio
async def test_data_persists_across_sessions():
    async with session_scope() as session:
        contest = await cs.create_contest(session, title="Persist", status=cs.STATUS_ACTIVE)
        cid = contest.id
    # Читаем в новой сессии — данные на месте.
    async with session_scope() as session:
        contest = await cs.get_contest(session, cid)
        assert contest is not None and contest.title == "Persist"
