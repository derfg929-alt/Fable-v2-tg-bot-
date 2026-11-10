"""Тесты для кнопок интерфейса и интеграции Grok AI."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram import types
from aiogram.enums import ChatType

from app.db.session import session_scope
from app.services.player_service import get_or_create_player, get_player
from app.services.economy_service import credit_coins
from app.handlers.social_moderation_handlers import (
    on_top_menu_rich,
    on_top_menu_active,
    on_top_menu_clans,
    on_top_menu_back,
)
from app.handlers.owner_handlers import on_owner_eco_reset_prompt
from app.handlers.games_handlers import handle_bait_claim
from app.handlers.ai_handlers import handle_ai_query, _should_trigger_ai
from app.services.ai_service import ask_grok


@pytest.mark.asyncio
async def test_top_buttons_callbacks():
    """Проверка работы всех кнопок меню топов."""
    async with session_scope() as session:
        reg = await get_or_create_player(session, telegram_id=987111, first_name="TopTester")
        await credit_coins(session, user_id=reg.player.telegram_id, amount=1000, reason="TestTop")

    # Mock Message and CallbackQuery
    chat_mock = types.Chat(id=-10012345, type=ChatType.SUPERGROUP, title="Top Chat")
    user_mock = types.User(id=987111, is_bot=False, first_name="TopTester")
    message_mock = AsyncMock(spec=types.Message)
    message_mock.chat = chat_mock
    message_mock.edit_text = AsyncMock()

    callback_mock = AsyncMock(spec=types.CallbackQuery)
    callback_mock.message = message_mock
    callback_mock.from_user = user_mock

    # Test rich top callback
    callback_mock.data = "top_menu_rich"
    await on_top_menu_rich(callback_mock)
    message_mock.edit_text.assert_called_once()
    assert "Топ богатых" in message_mock.edit_text.call_args[0][0]

    # Test active top callback
    message_mock.edit_text.reset_mock()
    callback_mock.data = "top_menu_active"
    await on_top_menu_active(callback_mock)
    message_mock.edit_text.assert_called_once()
    assert "Топ активных" in message_mock.edit_text.call_args[0][0]

    # Test clans top callback
    message_mock.edit_text.reset_mock()
    callback_mock.data = "top_menu_clans"
    await on_top_menu_clans(callback_mock)
    message_mock.edit_text.assert_called_once()
    assert "Топ кланов" in message_mock.edit_text.call_args[0][0]

    # Test back to menu callback
    message_mock.edit_text.reset_mock()
    callback_mock.data = "top_menu_back"
    await on_top_menu_back(callback_mock)
    message_mock.edit_text.assert_called_once()
    assert "Выберите рейтинг" in message_mock.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_owner_eco_reset_prompt_callback():
    """Проверка кнопки вызова сброса экономики из панели администратора."""
    chat_mock = types.Chat(id=8039111975, type=ChatType.PRIVATE)
    user_mock = types.User(id=8039111975, is_bot=False, first_name="Owner")
    message_mock = AsyncMock(spec=types.Message)
    message_mock.chat = chat_mock
    message_mock.edit_text = AsyncMock()

    callback_mock = AsyncMock(spec=types.CallbackQuery)
    callback_mock.message = message_mock
    callback_mock.from_user = user_mock
    callback_mock.data = "owner_eco_reset_prompt"

    await on_owner_eco_reset_prompt(callback_mock)
    message_mock.edit_text.assert_called_once()
    assert "ВНИМАНИЕ! ОПАСНАЯ ОПЕРАЦИЯ" in message_mock.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_bait_claim_no_name_error():
    """Проверка того, что кнопка дропа bait:claim корректно начисляет награду без NameError."""
    chat_mock = types.Chat(id=-100999, type=ChatType.SUPERGROUP, title="Bait Chat")
    user_mock = types.User(id=987222, is_bot=False, first_name="BaitHunter", username="baithunter")
    message_mock = AsyncMock(spec=types.Message)
    message_mock.chat = chat_mock
    message_mock.edit_text = AsyncMock()

    callback_mock = AsyncMock(spec=types.CallbackQuery)
    callback_mock.message = message_mock
    callback_mock.from_user = user_mock
    callback_mock.data = "bait:claim"
    callback_mock.answer = AsyncMock()

    await handle_bait_claim(callback_mock)
    callback_mock.answer.assert_called_once()
    assert "Вы перехватили дроп" in callback_mock.answer.call_args[0][0]

    # Проверяем, что монеты зачислились в БД
    async with session_scope() as session:
        player = await get_player(session, telegram_id=987222)
        assert player is not None
        assert player.wallet >= 25


@pytest.mark.asyncio
async def test_ai_trigger_detection():
    """Проверка триггеров на обращение к ИИ 'фабле'."""
    bot_id = 1234567
    now = datetime.now(timezone.utc)

    # Case 1: Текст содержит 'фабле'
    msg1 = types.Message(
        message_id=1,
        date=now,
        chat=types.Chat(id=1, type=ChatType.PRIVATE),
        text="Фабле, расскажи анекдот",
    )
    assert _should_trigger_ai(msg1, bot_id) is True

    # Case 2: Текст содержит 'фабле' в середине
    msg2 = types.Message(
        message_id=2,
        date=now,
        chat=types.Chat(id=1, type=ChatType.PRIVATE),
        text="Привет, как дела, фабле?",
    )
    assert _should_trigger_ai(msg2, bot_id) is True

    # Case 3: Обычное сообщение без триггера
    msg3 = types.Message(
        message_id=3,
        date=now,
        chat=types.Chat(id=1, type=ChatType.PRIVATE),
        text="профиль",
    )
    assert _should_trigger_ai(msg3, bot_id) is False

    # Case 4: Ответ на сообщение бота
    bot_user = types.User(id=bot_id, is_bot=True, first_name="Fable Bot")
    reply_to = types.Message(
        message_id=9,
        date=now,
        chat=types.Chat(id=1, type=ChatType.PRIVATE),
        from_user=bot_user,
        text="Чем могу помочь?",
    )
    msg4 = types.Message(
        message_id=10,
        date=now,
        chat=types.Chat(id=1, type=ChatType.PRIVATE),
        reply_to_message=reply_to,
        text="Объясни правила игры",
    )
    assert _should_trigger_ai(msg4, bot_id) is True


@pytest.mark.asyncio
async def test_ask_grok_without_key():
    """Проверка сервиса ask_grok при отсутствии ключа."""
    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_obj = MagicMock()
        mock_obj.grok_api_key_value = None
        mock_obj.effective_ai_api_key = None
        mock_obj.groq_api_key_value = None
        mock_settings.return_value = mock_obj

        resp = await ask_grok("Привет")
        assert "GROK_API_KEY" in resp


@pytest.mark.asyncio
async def test_settings_ai_keys_cleaning_and_whitespace():
    """Проверка очистки кавычек, пробелов и пустых значений в Settings."""
    from app.config import Settings
    from pydantic import SecretStr

    # 1. Ключ в кавычках и с пробелами
    s1 = Settings(GROQ_API_KEY=SecretStr('  "gsk_test_quote_123"  '))
    assert s1.effective_ai_api_key == "gsk_test_quote_123"
    assert s1.groq_api_key_value == "gsk_test_quote_123"

    # 2. Пустые кавычки и пробелы считаются None
    s2 = Settings(GROQ_API_KEY=SecretStr('  ""  '), GROK_API_KEY=SecretStr("   "))
    assert s2.effective_ai_api_key is None
    assert s2.groq_api_key_value is None
    assert s2.grok_api_key_value is None

    # 3. GROQ_API_KEY пустой, GROK_API_KEY заполнен
    s3 = Settings(GROQ_API_KEY=SecretStr(""), GROK_API_KEY=SecretStr("xai-live-key"))
    assert s3.effective_ai_api_key == "xai-live-key"
    assert "x.ai" in s3.effective_ai_api_url

    # 4. Передача обычных строк в валидатор
    s4 = Settings(GROQ_API_KEY="  'gsk_single_quotes'  ")
    assert s4.effective_ai_api_key == "gsk_single_quotes"

    # 5. Ключ с префиксом Bearer
    s5 = Settings(GROQ_API_KEY='Bearer "gsk_bearer_test_key"')
    assert s5.effective_ai_api_key == "gsk_bearer_test_key"
    assert s5.groq_api_key_value == "gsk_bearer_test_key"

    # 6. Устаревшая модель Groq заменяется на openai/gpt-oss-120b
    s6 = Settings(GROQ_API_KEY="gsk_valid", GROQ_MODEL="llama-3.3-70b-versatile")
    assert s6.effective_ai_model == "openai/gpt-oss-120b"

    s7 = Settings(GROQ_API_KEY="gsk_valid", GROQ_MODEL="llama-3.1-8b-instant")
    assert s7.effective_ai_model == "openai/gpt-oss-120b"

    # 7. Устаревшая модель xAI Grok заменяется на grok-4.7
    s8 = Settings(GROK_API_KEY="xai-key", GROK_MODEL="grok-beta")
    assert s8.effective_ai_model == "grok-4.7"
    assert s8.grok_model == "grok-4.7"

    s9 = Settings(GROK_API_KEY="xai-key", GROK_MODEL="grok-3")
    assert s9.grok_model == "grok-4.7"


@pytest.mark.asyncio
async def test_ask_grok_with_mocked_api():
    """Проверка вызова Grok API при успешном ответе."""
    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_obj = MagicMock()
        mock_obj.grok_api_key_value = "fake-key"
        mock_obj.grok_api_url = "https://api.x.ai/v1/chat/completions"
        mock_obj.grok_model = "grok-4.7"
        mock_settings.return_value = mock_obj

        # Create mock response context manager
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "Привет! Я Фабле ИИ."}}]
        })

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj = MagicMock()
        mock_session_obj.post.return_value = mock_post_cm

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session_cm):
            resp = await ask_grok("Как дела?", user_name="Игрок")
            assert resp == "Привет! Я Фабле ИИ."


@pytest.mark.asyncio
async def test_ask_groq_cloud_with_mocked_api():
    """Проверка вызова Groq Cloud API при передаче GROQ_API_KEY."""
    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_obj = MagicMock()
        mock_obj.grok_api_key_value = None
        mock_obj.effective_ai_api_key = "gsk_test_key"
        mock_obj.effective_ai_api_url = "https://api.groq.com/openai/v1/chat/completions"
        mock_obj.effective_ai_model = "openai/gpt-oss-120b"
        mock_settings.return_value = mock_obj

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "Привет от Groq AI!"}}]
        })

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj = MagicMock()
        mock_session_obj.post.return_value = mock_post_cm

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session_cm):
            resp = await ask_grok("Кто ты?", user_name="Тестер")
            assert resp == "Привет от Groq AI!"
            # Verify call args
            call_url = mock_session_obj.post.call_args[0][0]
            assert "api.groq.com" in call_url
            call_payload = mock_session_obj.post.call_args[1]["json"]
            assert call_payload["model"] == "openai/gpt-oss-120b"
            assert call_payload["max_tokens"] >= 800
            assert "Bearer gsk_test_key" == mock_session_obj.post.call_args[1]["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_ask_groq_cloud_404_model_fallback():
    """Проверка автоматического перехода на fallback-модель при ошибке 404."""
    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_obj = MagicMock()
        mock_obj.grok_api_key_value = None
        mock_obj.effective_ai_api_key = "gsk_test_key"
        mock_obj.effective_ai_api_url = "https://api.groq.com/openai/v1/chat/completions"
        mock_obj.effective_ai_model = "openai/gpt-oss-120b"
        mock_settings.return_value = mock_obj

        # Первая попытка: 404 (model not found)
        resp_404 = AsyncMock()
        resp_404.status = 404
        resp_404.text = AsyncMock(return_value='{"error":{"message":"model_not_found"}}')

        cm_404 = MagicMock()
        cm_404.__aenter__ = AsyncMock(return_value=resp_404)
        cm_404.__aexit__ = AsyncMock(return_value=None)

        # Вторая попытка с fallback-моделью openai/gpt-oss-20b: 200 OK
        resp_200 = AsyncMock()
        resp_200.status = 200
        resp_200.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "Ответ от резервной модели!"}}]
        })

        cm_200 = MagicMock()
        cm_200.__aenter__ = AsyncMock(return_value=resp_200)
        cm_200.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj = MagicMock()
        mock_session_obj.post.side_effect = [cm_404, cm_200]

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session_cm):
            resp = await ask_grok("Проверка", user_name="Тестер")
            assert resp == "Ответ от резервной модели!"
            assert mock_session_obj.post.call_count == 2
            second_call_payload = mock_session_obj.post.call_args_list[1][1]["json"]
            assert second_call_payload["model"] == "openai/gpt-oss-20b"


@pytest.mark.asyncio
async def test_ask_groq_rate_limit_and_error_handling():
    """Проверка retry при 429 и парсинга ответа с reasoning_content."""
    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_obj = MagicMock()
        mock_obj.effective_ai_api_key = "gsk_test_key"
        mock_obj.effective_ai_api_url = "https://api.groq.com/openai/v1/chat/completions"
        mock_obj.effective_ai_model = "openai/gpt-oss-120b"
        mock_settings.return_value = mock_obj

        # 1. Тест с reasoning_content вместо content
        resp_reasoning = AsyncMock()
        resp_reasoning.status = 200
        resp_reasoning.json = AsyncMock(return_value={
            "choices": [{"message": {"content": None, "reasoning_content": "Мысли и ответ"}}]
        })
        cm_reasoning = MagicMock()
        cm_reasoning.__aenter__ = AsyncMock(return_value=resp_reasoning)
        cm_reasoning.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj = MagicMock()
        mock_session_obj.post.return_value = cm_reasoning
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session_obj)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session_cm):
            resp = await ask_grok("Думай", user_name="Тестер")
            assert resp == "Мысли и ответ"

        # 2. Тест с 401 Unauthorized
        resp_401 = AsyncMock()
        resp_401.status = 401
        resp_401.json = AsyncMock(return_value={"error": {"message": "Invalid API key"}})
        cm_401 = MagicMock()
        cm_401.__aenter__ = AsyncMock(return_value=resp_401)
        cm_401.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj.post.return_value = cm_401
        with patch("aiohttp.ClientSession", return_value=mock_session_cm):
            resp = await ask_grok("Привет", user_name="Тестер")
            assert "Ошибка авторизации" in resp

        # 3. Тест с 429 Rate Limit (исчерпаны попытки)
        resp_429 = AsyncMock()
        resp_429.status = 429
        resp_429.headers = {}
        resp_429.json = AsyncMock(return_value={"error": {"message": "Rate limit exceeded"}})
        cm_429 = MagicMock()
        cm_429.__aenter__ = AsyncMock(return_value=resp_429)
        cm_429.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj.post.return_value = cm_429
        with patch("aiohttp.ClientSession", return_value=mock_session_cm), patch("asyncio.sleep", AsyncMock()):
            resp = await ask_grok("Привет еще раз", user_name="Тестер")
            assert resp == "Слишком много запросов, подождите минуту"

        # 4. Тест xAI Grok 400 invalid-argument с ошибкой ключа
        mock_obj.effective_ai_api_url = "https://api.x.ai/v1/chat/completions"
        resp_400_xai = AsyncMock()
        resp_400_xai.status = 400
        resp_400_xai.json = AsyncMock(return_value={
            "code": "invalid-argument",
            "error": "Incorrect API key provided. You can obtain an API key from https://console.x.ai."
        })
        cm_400_xai = MagicMock()
        cm_400_xai.__aenter__ = AsyncMock(return_value=resp_400_xai)
        cm_400_xai.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj.post.return_value = cm_400_xai
        with patch("aiohttp.ClientSession", return_value=mock_session_cm):
            resp = await ask_grok("Тест ключа", user_name="Тестер")
            assert "Ошибка авторизации" in resp
            assert "GROK_API_KEY (xAI)" in resp


@pytest.mark.asyncio
async def test_dispatcher_callback_routing_not_intercepted_by_fallback():
    """Проверяем, что create_dispatcher() корректно доставляет CallbackQuery до нужных хэндлеров,
    а не перехватывает их глобальным фоллбэком.
    """
    from aiogram import Bot
    from app.main import create_dispatcher

    bot = Bot(token="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_1234567")
    bot.session = MagicMock()
    bot.session.side_effect = AsyncMock(return_value=True)

    dp = create_dispatcher()

    # 1. Проверяем кнопку 'top_menu_rich'
    raw_top_update = {
        "update_id": 201,
        "callback_query": {
            "id": "cb_top_1",
            "from": {"id": 111222333, "is_bot": False, "first_name": "ButtonTester"},
            "message": {
                "message_id": 50,
                "date": 1700000000,
                "chat": {"id": -100998877, "type": "supergroup", "title": "Test Group"},
                "text": "Топ игроков",
            },
            "chat_instance": "ci_top",
            "data": "top_menu_rich",
        },
    }
    update = types.Update.model_validate(raw_top_update, context={"bot": bot})
    await dp.feed_update(bot=bot, update=update)

    called_methods = [c[0][1].__class__.__name__ for c in bot.session.call_args_list]
    # Должен быть вызван EditMessageText от хэндлера on_top_menu_rich, а не только AnswerCallbackQuery
    assert "EditMessageText" in called_methods

    # 2. Проверяем кнопку владельца 'owner_menu_stats'
    bot.session.reset_mock()
    raw_owner_update = {
        "update_id": 202,
        "callback_query": {
            "id": "cb_owner_1",
            "from": {"id": 8039111975, "is_bot": False, "first_name": "OwnerTester"},
            "message": {
                "message_id": 51,
                "date": 1700000000,
                "chat": {"id": 8039111975, "type": "private"},
                "text": "Панель управления Fable",
            },
            "chat_instance": "ci_owner",
            "data": "owner_menu_stats",
        },
    }
    update_owner = types.Update.model_validate(raw_owner_update, context={"bot": bot})
    await dp.feed_update(bot=bot, update=update_owner)

    owner_called_methods = [c[0][1].__class__.__name__ for c in bot.session.call_args_list]
    assert "EditMessageText" in owner_called_methods

    # 3. Проверяем неизвестную кнопку — должна сработать fallback_callback_router и ответить
    bot.session.reset_mock()
    raw_unknown_update = {
        "update_id": 203,
        "callback_query": {
            "id": "cb_unknown_1",
            "from": {"id": 111222333, "is_bot": False, "first_name": "ButtonTester"},
            "message": {
                "message_id": 52,
                "date": 1700000000,
                "chat": {"id": -100998877, "type": "supergroup"},
                "text": "Любой текст",
            },
            "chat_instance": "ci_unkn",
            "data": "some_completely_unknown_callback",
        },
    }
    update_unknown = types.Update.model_validate(raw_unknown_update, context={"bot": bot})
    await dp.feed_update(bot=bot, update=update_unknown)

    unknown_called_methods = [c[0][1].__class__.__name__ for c in bot.session.call_args_list]
    assert "AnswerCallbackQuery" in unknown_called_methods
    assert "EditMessageText" not in unknown_called_methods


@pytest.mark.asyncio
async def test_ask_grok_dynamic_env_detection(tmp_path, monkeypatch):
    """Проверка подхвата ключа из os.environ и файла .env на лету без возврата заглушки."""
    from app.config import get_settings

    # Сбрасываем кэш и окружение
    get_settings.cache_clear()
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)

    # 1. При отсутствии ключей возвращается сообщение-заглушка
    stub_response = await ask_grok("Привет")
    assert "Мой модуль искусственного интеллекта готов к работе" in stub_response

    # 2. Появление ключа в os.environ подхватывается сразу
    monkeypatch.setenv("GROQ_API_KEY", "gsk_dynamic_env_test_key")

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "choices": [{"message": {"content": "Ответ от динамического Groq!"}}]
    })

    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session_obj = MagicMock()
    mock_session_obj.post.return_value = mock_post_cm

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session_obj)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session_cm):
        resp_env = await ask_grok("Вопрос с env")
        assert resp_env == "Ответ от динамического Groq!"
        # Проверяем, что в Authorization попал новый ключ
        auth_header = mock_session_obj.post.call_args[1]["headers"]["Authorization"]
        assert auth_header == "Bearer gsk_dynamic_env_test_key"

    # 3. Появление ключа в .env файле
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    get_settings.cache_clear()

    env_file = tmp_path / ".env"
    env_file.write_text("GROK_API_KEY=xai_dynamic_file_key\n")

    with monkeypatch.context() as m:
        m.chdir(tmp_path)
        get_settings.cache_clear()

        mock_response_xai = AsyncMock()
        mock_response_xai.status = 200
        mock_response_xai.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "Ответ от xAI Grok из .env!"}}]
        })

        mock_post_cm_xai = MagicMock()
        mock_post_cm_xai.__aenter__ = AsyncMock(return_value=mock_response_xai)
        mock_post_cm_xai.__aexit__ = AsyncMock(return_value=None)

        mock_session_obj_xai = MagicMock()
        mock_session_obj_xai.post.return_value = mock_post_cm_xai

        mock_session_cm_xai = MagicMock()
        mock_session_cm_xai.__aenter__ = AsyncMock(return_value=mock_session_obj_xai)
        mock_session_cm_xai.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=mock_session_cm_xai):
            resp_file = await ask_grok("Вопрос с .env")
            assert resp_file == "Ответ от xAI Grok из .env!"
            auth_header_xai = mock_session_obj_xai.post.call_args[1]["headers"]["Authorization"]
            assert auth_header_xai == "Bearer xai_dynamic_file_key"
