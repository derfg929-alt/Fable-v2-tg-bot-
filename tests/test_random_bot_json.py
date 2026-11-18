"""Тесты бота розыгрышей на JSON-хранилище (подпроект fable_random_bot)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from fable_random_bot import admin_flow, database, draw, keyboards, utils
from fable_random_bot.handlers import draw as draw_handlers
from fable_random_bot.handlers import user as user_handlers


class FakeBot:
    """Минимальная замена aiogram.Bot для проверки логики без сети."""

    def __init__(self, bot_status="administrator", can_post_messages=True, member_status="creator"):
        self.id = 1
        self.username = "mybot"
        self.sent = []
        self.bot_status = bot_status
        self.can_post_messages = can_post_messages
        # Статус, который вернётся для любого пользователя в get_chat_member.
        self.member_status = member_status

    async def get_me(self):
        return SimpleNamespace(username=self.username)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return SimpleNamespace(message_id=len(self.sent))

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=-100123, username="chan", title="Канал")

    async def get_chat_member(self, chat_id, user_id):
        if user_id == self.id:
            return SimpleNamespace(status=self.bot_status, can_post_messages=self.can_post_messages)
        return SimpleNamespace(status=self.member_status, can_post_messages=True)


class FakeChecker:
    def __init__(self, subscribed=True):
        self.subscribed = subscribed

    async def is_subscribed(self, channel, user_id):
        return self.subscribed


class FakeMessage:
    def __init__(self, bot=None):
        self.bot = bot
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


class FakeCallback:
    def __init__(self, data, user_id=10, username="u", bot=None, subscribed=True):
        self.data = data
        self.bot = bot or FakeBot()
        self.from_user = SimpleNamespace(id=user_id, username=username)
        self.message = FakeMessage(self.bot)
        self.alerted = []

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerted.append(text)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Изолируем JSON-базу в tmp_path и сбрасываем кэш рейтинга."""
    db_file = tmp_path / "database.json"
    monkeypatch.setattr(database.config, "DB_PATH", str(db_file))
    database.invalidate_leaderboard_cache()
    yield db_file
    database.invalidate_leaderboard_cache()


async def test_template_created_and_atomic(tmp_path, isolated_db):
    data = await database.load_data()
    assert set(data) >= {"users", "contests", "admin_state", "code_word_attempts"}
    assert isolated_db.exists()
    # Временный файл не остаётся после записи.
    leftovers = list(Path(isolated_db).parent.glob("*.tmp"))
    assert leftovers == []


async def test_atomic_write_survives_corrupt_file(isolated_db):
    isolated_db.write_text("{ broken json", encoding="utf-8")
    data = await database.load_data()
    assert data["users"] == {}


async def test_get_user_creates_and_updates(isolated_db):
    user = await database.get_user(5, "vasya")
    assert user["username"] == "vasya"
    assert user["tickets"] == 0
    await database.add_tickets(5, 3)
    user = await database.get_user(5)
    assert user["tickets"] == 3


async def test_add_participant_is_idempotent(isolated_db):
    await database.create_contest({"id": "1", "participants": [], "settings": {}})
    assert await database.add_participant("1", 10) is True
    assert await database.add_participant("1", 10) is False
    contest = await database.get_contest("1")
    assert contest["participants"] == ["10"]


async def test_referral_credits_once(isolated_db):
    await database.create_contest({"id": "1", "participants": [], "settings": {}})
    assert await database.add_referral(100, 200, "1") is True
    assert await database.add_referral(100, 200, "1") is False
    user = await database.get_user(100)
    assert user["referrals"] == ["200"]


async def test_pending_referral_roundtrip(isolated_db):
    await database.set_pending_referral(7, 99)
    assert await database.pop_pending_referral(7) == 99
    assert await database.pop_pending_referral(7) is None


async def test_code_word_registered_once(isolated_db):
    assert await database.register_code_word("1", 5, "PROMO") is True
    assert await database.register_code_word("1", 5, "PROMO") is False
    assert await database.get_code_word_attempts("1", 5) == ["PROMO"]


async def test_next_contest_id(isolated_db):
    data = await database.load_data()
    assert database.next_contest_id(data) == "1"
    await database.create_contest({"id": "1", "participants": [], "settings": {}})
    await database.create_contest({"id": "5", "participants": [], "settings": {}})
    data = await database.load_data()
    assert database.next_contest_id(data) == "6"


# --- Мастер создания конкурса --------------------------------------------

async def test_admin_flow_happy_path(isolated_db):
    reply = await admin_flow.start_creation(42)
    assert "название" in reply.text.lower()

    reply = await admin_flow.handle_text(42, "iPhone 15", bot=None)
    assert "приз" in reply.text.lower()

    reply = await admin_flow.handle_text(42, "Телефон", bot=None)
    assert "канал" in reply.text.lower()

    # Без канала — знак «-».
    reply = await admin_flow.handle_text(42, "-", bot=None)
    assert "спонсор" in reply.text.lower()

    reply = await admin_flow.handle_callback(42, "sponsors_done", bot=None)
    assert "формат" in reply.text.lower() or "заверш" in reply.text.lower()

    reply = await admin_flow.handle_text(42, "31.12.2026 23:59", bot=None)
    assert "победител" in reply.text.lower()

    reply = await admin_flow.handle_text(42, "2", bot=None)
    assert "настройки" in reply.text.lower()

    reply = await admin_flow.handle_callback(42, "finish_contest", bot=None)
    assert "создан" in reply.text.lower()

    contests = await database.list_contests()
    assert "1" in contests
    assert contests["1"]["winners_count"] == 2
    assert await database.get_admin_state(42) == {}


async def test_admin_flow_invalid_date(isolated_db):
    await admin_flow.start_creation(42)
    await admin_flow.handle_text(42, "T", bot=None)
    await admin_flow.handle_text(42, "P", bot=None)
    await admin_flow.handle_text(42, "-", bot=None)
    await admin_flow.handle_callback(42, "sponsors_done", bot=None)
    reply = await admin_flow.handle_text(42, "не-дата", bot=None)
    assert "формат" in reply.text.lower()


async def test_admin_flow_cancel(isolated_db):
    await admin_flow.start_creation(42)
    reply = await admin_flow.handle_callback(42, "cancel", bot=None)
    assert "отмен" in reply.text.lower()
    assert await database.get_admin_state(42) == {}


async def test_admin_flow_code_words_toggle(isolated_db):
    await admin_flow.start_creation(42)
    await admin_flow.handle_text(42, "T", bot=None)
    await admin_flow.handle_text(42, "P", bot=None)
    await admin_flow.handle_text(42, "-", bot=None)
    await admin_flow.handle_callback(42, "sponsors_done", bot=None)
    await admin_flow.handle_text(42, "31.12.2026 23:59", bot=None)
    await admin_flow.handle_text(42, "1", bot=None)

    reply = await admin_flow.handle_callback(42, "toggle_codes", bot=None)
    assert "кодовые" in reply.text.lower()
    reply = await admin_flow.handle_text(42, "PROMO2026:2, SALE:3", bot=None)
    assert "настройки" in reply.text.lower()
    state = await database.get_admin_state(42)
    assert state["temp_data"]["settings"]["code_words"] == [
        {"word": "PROMO2026", "tickets": 2},
        {"word": "SALE", "tickets": 3},
    ]


# --- Жеребьёвка -----------------------------------------------------------

def _contest(participants, tickets, winners_count, reserve_count):
    contest = {
        "id": "1",
        "participants": [str(p) for p in participants],
        "winners_count": winners_count,
        "settings": {"reserve_count": reserve_count},
    }
    users = {
        str(p): {"contest_tickets": {"1": tickets.get(p, tickets.get(str(p), 1))}}
        for p in participants
    }
    return contest, users


def test_draw_weights_favour_more_tickets():
    # 9 участников по 1 билету и один с 1000 — почти всегда побеждает он.
    contest, users = _contest([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], {i: 1 for i in range(1, 11)}, 1, 0)
    users["10"]["contest_tickets"]["1"] = 1000
    wins = 0
    for _ in range(50):
        result = draw.draw_winners(contest, users, 1, 0)
        if result["winners"] == ["10"]:
            wins += 1
    assert wins > 40


def test_draw_no_duplicate_winners():
    contest, users = _contest([1, 2, 3], {1: 5, 2: 5, 3: 5}, 3, 2)
    result = draw.draw_winners(contest, users, 3, 2)
    assert len(result["winners"]) == 3
    assert len(set(result["winners"])) == 3
    assert len(result["reserve"]) == 0  # участников всего 3


def test_draw_reserves_and_transparency():
    contest, users = _contest([1, 2, 3, 4, 5], {i: 1 for i in range(1, 6)}, 2, 2)
    result = draw.draw_winners(contest, users, 2, 2)
    assert len(result["winners"]) == 2
    assert len(result["reserve"]) == 2
    tr = result["transparency"]
    assert tr["total_tickets"] == 5
    assert len(tr["sha256"]) == 64
    assert tr["salt"]


def test_draw_empty_pool():
    contest, users = _contest([], {}, 1, 0)
    result = draw.draw_winners(contest, users, 1, 0)
    assert result["winners"] == []


# --- Реферальный диплинк /start ref_<id> ----------------------------------

async def test_start_ref_link_stores_pending_referral(isolated_db):
    from aiogram.filters import CommandObject
    from fable_random_bot.handlers import start as start_handlers

    message = FakeMessage(FakeBot())
    message.from_user = SimpleNamespace(id=20, username="u")
    command = CommandObject(prefix="/", command="start", args="ref_10")
    await start_handlers.cmd_start(message, command)
    assert await database.pop_pending_referral(20) == 10


async def test_start_ignores_self_referral(isolated_db):
    from aiogram.filters import CommandObject
    from fable_random_bot.handlers import start as start_handlers

    message = FakeMessage(FakeBot())
    message.from_user = SimpleNamespace(id=20, username="u")
    command = CommandObject(prefix="/", command="start", args="ref_20")
    await start_handlers.cmd_start(message, command)
    assert await database.pop_pending_referral(20) is None


# --- draw_winner (один победитель по конкурсу) ----------------------------

async def test_draw_winner_returns_participant(isolated_db):
    await _make_contest()
    for uid in (10, 20, 30):
        await database.add_participant("1", uid)
    winner = await utils.draw_winner("1")
    assert winner in {"10", "20", "30"}


async def test_draw_winner_unknown_contest(isolated_db):
    assert await utils.draw_winner("999") is None


async def test_draw_winner_no_participants(isolated_db):
    await _make_contest()
    assert await utils.draw_winner("1") is None


async def test_draw_winner_favours_more_tickets(isolated_db):
    await _make_contest()
    await database.add_participant("1", 10)
    await database.add_participant("1", 20)
    # Участнику 20 даём большое преимущество в билетах.
    await database.add_contest_tickets(20, "1", 1000)
    wins = 0
    for _ in range(60):
        if await utils.draw_winner("1") == "20":
            wins += 1
    assert wins > 50


# --- Рейтинг приглашающих -------------------------------------------------

async def test_leaderboard_counts_only_participants(isolated_db):
    await database.create_contest({"id": "1", "participants": [], "settings": {}})
    for uid in (10, 20, 30):
        await database.add_participant("1", uid)
    await database.add_referral(10, 20, "1")
    await database.add_referral(10, 30, "1")
    await database.add_referral(20, 999, "1")  # 999 не участник — не считается

    board = await database.get_leaderboard("1", 10)
    assert board[0]["user_id"] == 10
    assert board[0]["invited"] == 2


def test_keyboards_reflect_state():
    temp = {"settings": {"tickets_per_ref": 7, "code_words": [{"word": "X"}], "leaderboard": True}}
    markup = keyboards.settings_keyboard(temp)
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Билетов за реферала: 7" in t for t in texts)
    assert any("Кодовые слова: Вкл" in t for t in texts)
    assert any("Рейтинг приглашающих: Вкл" in t for t in texts)


def test_main_menu_create_visible_to_everyone():
    """Создавать конкурсы может любой пользователь — кнопка видна всем."""
    for uid in (1, 999, 123456):
        texts = [b.text for row in keyboards.main_menu(uid).keyboard for b in row]
        assert keyboards.BTN_CREATE in texts
        assert keyboards.BTN_ACTIVE in texts
        assert keyboards.BTN_MY in texts


# --- Поток участия (join) -------------------------------------------------

async def _make_contest(**overrides):
    contest = {
        "id": "1",
        "title": "Test",
        "status": "active",
        "channel": None,
        "sponsors": [],
        "participants": [],
        "winners_count": 1,
        "settings": {
            "tickets_per_ref": 5,
            "code_words": [{"word": "PROMO", "tickets": 2}],
            "share_button": True,
            "reserve_count": 0,
        },
    }
    contest.update(overrides)
    await database.create_contest(contest)
    return contest


async def test_join_requires_subscription(isolated_db, monkeypatch):
    await _make_contest(channel="@chan")
    monkeypatch.setattr(user_handlers, "checker", FakeChecker(subscribed=False))
    callback = FakeCallback("join_1", user_id=10)
    await user_handlers.on_join(callback)
    contest = await database.get_contest("1")
    assert contest["participants"] == []
    assert any("подписаться" in a.lower() for a in callback.message.answers)


async def test_join_success_and_referral_credit(isolated_db, monkeypatch):
    await _make_contest()
    monkeypatch.setattr(user_handlers, "checker", FakeChecker(subscribed=True))
    # Пользователь 20 пришёл по ссылке пользователя 10.
    await database.set_pending_referral(20, 10)
    callback = FakeCallback("join_1", user_id=20)
    await user_handlers.on_join(callback)

    contest = await database.get_contest("1")
    assert contest["participants"] == ["20"]
    assert await database.get_contest_tickets(20, "1") == 1

    referrer = await database.get_user(10)
    assert referrer["referrals"] == ["20"]
    assert await database.get_contest_tickets(10, "1") == 5
    # pending-реферал очищен.
    assert await database.pop_pending_referral(20) is None


async def test_join_twice_rejected(isolated_db, monkeypatch):
    await _make_contest()
    monkeypatch.setattr(user_handlers, "checker", FakeChecker(subscribed=True))
    await user_handlers.on_join(FakeCallback("join_1", user_id=20))
    second = FakeCallback("join_1", user_id=20)
    await user_handlers.on_join(second)
    assert any("уже участвуешь" in a.lower() for a in second.alerted)
    contest = await database.get_contest("1")
    assert contest["participants"] == ["20"]


async def test_code_word_grants_tickets(isolated_db, monkeypatch):
    await _make_contest()
    monkeypatch.setattr(user_handlers, "checker", FakeChecker(subscribed=True))
    await user_handlers.on_join(FakeCallback("join_1", user_id=20))

    message = FakeMessage(FakeBot())
    message.text = "promo"
    message.from_user = SimpleNamespace(id=20, username="u")
    await user_handlers.on_text(message)
    assert any("Кодовое слово принято" in a for a in message.answers)
    assert await database.get_contest_tickets(20, "1") == 3  # 1 базовый + 2 за слово

    # Повторный ввод не начисляет ещё раз.
    message.answers = []
    await user_handlers.on_text(message)
    assert any("уже использовал" in a for a in message.answers)
    assert await database.get_contest_tickets(20, "1") == 3


# --- Завершение конкурса --------------------------------------------------

async def test_finish_contest_publishes_and_sets_winners(isolated_db):
    await _make_contest(channel="@chan", channel_id=-100, winners_count=1)
    for uid in (10, 20, 30):
        await database.add_participant("1", uid)
    bot = FakeBot()
    contest = await draw_handlers.finish_contest(bot, "1")
    assert contest["status"] == "finished"
    assert len(contest["winners"]) == 1
    assert contest["transparency"]["sha256"]
    # Опубликовано в канал и победителю в личку.
    assert any(m["chat_id"] == -100 for m in bot.sent)
    assert any(m["chat_id"] == int(contest["winners"][0]) for m in bot.sent)


async def test_check_and_finish_due_contest(isolated_db):
    await _make_contest(ends_at="2000-01-01T00:00:00", channel_id=None)
    await database.add_participant("1", 10)
    await draw_handlers.check_and_finish(FakeBot())
    contest = await database.get_contest("1")
    assert contest["status"] == "finished"


async def test_check_and_finish_skips_future(isolated_db):
    await _make_contest(ends_at="2999-01-01T00:00:00")
    await database.add_participant("1", 10)
    await draw_handlers.check_and_finish(FakeBot())
    contest = await database.get_contest("1")
    assert contest["status"] == "active"


# --- Маршрутизация текста (мастер конкурса vs кодовые слова) ---------------

async def test_wizard_text_skipped_without_active_step(isolated_db):
    """Текст без активного шага мастера пропускается дальше (кодовые слова)."""
    from aiogram.dispatcher.event.bases import SkipHandler
    from fable_random_bot.handlers import admin as admin_handlers

    message = FakeMessage(FakeBot())
    message.text = "PROMO"
    message.from_user = SimpleNamespace(id=999, username="u")
    with pytest.raises(SkipHandler):
        await admin_handlers.on_admin_text(message)


async def test_wizard_text_consumed_for_non_admin(isolated_db):
    """Мастер доступен любому пользователю, не только ADMIN_IDS."""
    from fable_random_bot.handlers import admin as admin_handlers

    await admin_flow.start_creation(999)  # 999 не админ
    message = FakeMessage(FakeBot())
    message.text = "Мой конкурс"
    message.from_user = SimpleNamespace(id=999, username="u")
    await admin_handlers.on_admin_text(message)
    assert any("приз" in a.lower() for a in message.answers)


# --- Создание конкурса любым пользователем --------------------------------

async def test_anyone_can_create_contest_without_channel(isolated_db):
    """Не-админ создаёт конкурс без канала — сохраняется с owner_id."""
    await admin_flow.start_creation(999)
    await admin_flow.handle_text(999, "Народный конкурс", bot=None)
    await admin_flow.handle_text(999, "Приз", bot=None)
    await admin_flow.handle_text(999, "-", bot=None)  # без канала
    await admin_flow.handle_callback(999, "sponsors_done", bot=None)
    await admin_flow.handle_text(999, "31.12.2026 23:59", bot=None)
    await admin_flow.handle_text(999, "1", bot=None)
    reply = await admin_flow.handle_callback(999, "finish_contest", bot=None)
    assert "создан" in reply.text.lower()
    contest = await database.get_contest("1")
    assert contest["owner_id"] == 999
    assert contest["channel_id"] is None


# --- Публикация в канале: проверка владельца/прав -------------------------

async def _reach_channel_step(user_id: int) -> None:
    await admin_flow.start_creation(user_id)
    await admin_flow.handle_text(user_id, "T", bot=None)
    await admin_flow.handle_text(user_id, "P", bot=None)


async def test_channel_requires_user_to_be_admin(isolated_db):
    """Обычный участник канала не может опубликовать там конкурс."""
    await _reach_channel_step(999)
    bot = FakeBot(member_status="member")
    reply = await admin_flow.handle_text(999, "@chan", bot)
    assert "не владелец" in reply.text.lower()
    state = await database.get_admin_state(999)
    assert state["step"] == "waiting_channel"  # шаг не пройден
    assert not state["temp_data"].get("channel_id")


async def test_channel_accepted_when_user_is_owner_and_bot_admin(isolated_db):
    await _reach_channel_step(999)
    bot = FakeBot(member_status="creator", bot_status="administrator", can_post_messages=True)
    reply = await admin_flow.handle_text(999, "@chan", bot)
    assert "спонсор" in reply.text.lower()  # перешли к следующему шагу
    state = await database.get_admin_state(999)
    assert state["temp_data"]["channel_id"] == -100123


async def test_channel_rejected_when_bot_not_admin(isolated_db):
    """Бот не админ канала — публиковать нельзя."""
    await _reach_channel_step(999)
    bot = FakeBot(member_status="creator", bot_status="member")
    reply = await admin_flow.handle_text(999, "@chan", bot)
    assert "бот не админ" in reply.text.lower()


async def test_channel_rejected_without_post_rights(isolated_db):
    """Бот админ, но без права публикации сообщений."""
    await _reach_channel_step(999)
    bot = FakeBot(member_status="creator", bot_status="administrator", can_post_messages=False)
    reply = await admin_flow.handle_text(999, "@chan", bot)
    assert "публик" in reply.text.lower()


async def test_contest_publishes_to_channel_when_allowed(isolated_db):
    await _reach_channel_step(999)
    bot = FakeBot(member_status="creator", bot_status="administrator", can_post_messages=True)
    await admin_flow.handle_text(999, "@chan", bot)
    await admin_flow.handle_callback(999, "sponsors_done", bot)
    await admin_flow.handle_text(999, "31.12.2026 23:59", bot)
    await admin_flow.handle_text(999, "1", bot)
    reply = await admin_flow.handle_callback(999, "finish_contest", bot)
    assert "создан" in reply.text.lower()
    assert not any("не удалось опубликовать" in m["text"].lower() for m in bot.sent)
    assert any(m["chat_id"] == -100123 for m in bot.sent)  # пост в канал
