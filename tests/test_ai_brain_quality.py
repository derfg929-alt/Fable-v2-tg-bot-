"""Тесты качества встроенного офлайн-движка Fable AI.

Проверяют, что бот даёт содержательные ответы (без заглушек) как на общие
вопросы, так и на вопросы про самого бота Fable, когда внешний API недоступен.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_knowledge import build_offline_response, is_bot_topic, try_math
from app.services.ai_service import ask_grok

# Фразы-признаки «заглушки», которых быть не должно в осмысленных ответах.
STUB_MARKERS = (
    "здесь важно учитывать ключевые факторы",
    "в любой сложной теме главное",
    "разложить всё по полочкам",
    "уточни деталь, и я с удовольствием",
)


def _assert_not_stub(text: str) -> None:
    for marker in STUB_MARKERS:
        assert marker not in text, f"Ответ содержит заглушку: {marker!r} в {text!r}"


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Какая столица Франции?", "Париж"),
        ("Что такое фотосинтез?", "Фотосинтез"),
        ("Почему небо голубое?", "рассеян"),
        ("Кто написал Войну и мир?", "Толстой"),
        ("Объясни теорему Пифагора", "Пифагор"),
        ("Что такое блокчейн?", "Блокчейн"),
        ("Что такое Python?", "Python"),
        ("Что такое нейросеть?", "нейрон"),
        ("Расскажи интересный факт", "факт"),
        ("Что делать если грустно?", "дыхание"),
    ],
)
def test_offline_general_questions(question, expected):
    """Общие вопросы (не про бота) получают конкретный ответ, а не заглушку."""
    answer = build_offline_response(question, user_name="Игрок")
    assert expected.lower() in answer.lower()
    _assert_not_stub(answer)


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Как заработать монеты?", "заработать"),
        ("Что такое банк и какой процент?", "5%"),
        ("Как работает биржа?", "биржа"),
        ("Как вступить в клан?", "клан"),
        ("Как купить бизнес?", "бизнес"),
        ("Что такое майнинг?", "ферм"),
        ("Как работает космодром?", "космодром"),
        ("Какие есть команды?", "команд"),
        ("Как работает шахта?", "шахт"),
        ("Что такое кейсы?", "кейс"),
    ],
)
def test_offline_bot_questions(question, expected):
    """Вопросы про бота Fable получают точную справку по командам и механике."""
    answer = build_offline_response(question, user_name="Игрок")
    assert expected.lower() in answer.lower()
    _assert_not_stub(answer)


def test_offline_math():
    """Калькулятор корректно считает примеры."""
    assert "100" in try_math("Сколько будет 25 * 4?")
    assert "42" in try_math("посчитай 6 * 7")
    assert try_math("привет как дела") is None


def test_offline_greeting_and_who():
    """Приветствие и вопрос «кто ты» получают осмысленный ответ."""
    greet = build_offline_response("Привет", user_name="Вася")
    assert "Вася" in greet
    who = build_offline_response("Кто ты?", user_name="Вася")
    assert "Fable" in who or "Фабле" in who


def test_is_bot_topic_detection():
    """Тематика бота распознаётся, а общие вопросы — нет."""
    assert is_bot_topic("как заработать монеты в боте") is True
    assert is_bot_topic("что такое биржа") is True
    assert is_bot_topic("как работает блокчейн") is False
    assert is_bot_topic("почему небо голубое") is False
    assert is_bot_topic("расскажи анекдот") is False


@pytest.mark.asyncio
async def test_ask_grok_offline_no_stubs():
    """ask_grok без ключа отвечает содержательно по боту и общим темам, без заглушек."""
    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_obj = MagicMock()
        mock_obj.grok_api_key_value = None
        mock_obj.effective_ai_api_key = None
        mock_obj.groq_api_key_value = None
        mock_settings.return_value = mock_obj

        for question in [
            "Что такое фотосинтез?",
            "Как заработать монеты?",
            "Сколько будет 25 * 4?",
            "Какая столица Франции?",
            "Как работает биржа?",
        ]:
            answer = await ask_grok(question, user_name="Игрок")
            _assert_not_stub(answer)
            assert len(answer) > 30


@pytest.mark.asyncio
async def test_ask_grok_empty_online_falls_back_offline():
    """Если внешний провайдер вернул пустой ответ, используется офлайн-движок, а не заглушка."""
    with patch("app.services.ai_service.get_settings") as mock_settings:
        mock_obj = MagicMock()
        mock_obj.grok_api_key_value = "fake-key"
        mock_obj.grok_api_url = "https://api.x.ai/v1/chat/completions"
        mock_obj.grok_model = "grok-4.7"
        mock_obj.effective_ai_api_key = "fake-key"
        mock_obj.effective_ai_api_url = "https://api.x.ai/v1/chat/completions"
        mock_obj.effective_ai_model = "grok-4.7"
        mock_settings.return_value = mock_obj

        empty_response = AsyncMock()
        empty_response.status = 200
        empty_response.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=empty_response)
        cm.__aexit__ = AsyncMock(return_value=None)

        session_obj = MagicMock()
        session_obj.post.return_value = cm
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session_obj)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("aiohttp.ClientSession", return_value=session_cm):
            answer = await ask_grok("Какая столица Франции?", user_name="Игрок")
            assert "Париж" in answer
            _assert_not_stub(answer)
