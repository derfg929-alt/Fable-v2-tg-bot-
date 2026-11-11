"""Тесты разрешения подключения к ИИ (xAI Grok / Groq Cloud) и фоллбеков.

Проверяют, что бот корректно выбирает провайдера, URL и модель, а также
переключается на резервные модели и офлайн-движок при сбоях.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.ai_service as ai_service
from app.services.ai_service import ask_grok, resolve_ai_connection


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Изолируем окружение: убираем ключи и сбрасываем кэш настроек."""
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    ai_service.get_settings.cache_clear()
    yield
    ai_service.get_settings.cache_clear()


def test_resolve_grok_provider(monkeypatch):
    """При заданном GROK_API_KEY выбирается xAI, модель grok-4.7."""
    monkeypatch.setenv("GROK_API_KEY", "xai-live-key-123456")
    conn = resolve_ai_connection()
    assert conn["provider"] == "grok"
    assert conn["api_key"] == "xai-live-key-123456"
    assert conn["url"] == "https://api.x.ai/v1/chat/completions"
    assert conn["model"] == "grok-4.7"
    assert conn["fallback_models"][0] == "grok-4.7"
    assert "grok-4" in conn["fallback_models"]


def test_resolve_groq_provider(monkeypatch):
    """При заданном GROQ_API_KEY выбирается Groq Cloud с корректным URL."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_123456")
    conn = resolve_ai_connection()
    assert conn["provider"] == "groq"
    assert conn["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert conn["model"] == "openai/gpt-oss-120b"


def test_resolve_grok_priority_over_groq(monkeypatch):
    """Если заданы оба ключа, приоритет у xAI Grok."""
    monkeypatch.setenv("GROK_API_KEY", "xai-live-key-123456")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_123456")
    conn = resolve_ai_connection()
    assert conn["provider"] == "grok"
    assert "x.ai" in conn["url"]


def test_resolve_no_keys():
    """Без ключей провайдер не определяется (работает офлайн-движок)."""
    conn = resolve_ai_connection()
    assert conn["provider"] is None
    assert conn["api_key"] is None
    assert conn["fallback_models"] == []


def test_resolve_key_cleaning(monkeypatch):
    """Ключ очищается от кавычек, пробелов и префикса Bearer."""
    monkeypatch.setenv("GROK_API_KEY", '  Bearer "xai-quoted-key"  ')
    conn = resolve_ai_connection()
    assert conn["api_key"] == "xai-quoted-key"


@pytest.mark.asyncio
async def test_ask_grok_uses_xai_endpoint_and_model(monkeypatch):
    """ask_grok отправляет запрос на xAI с моделью grok-4.7 и ключом Grok."""
    monkeypatch.setenv("GROK_API_KEY", "xai-live-key-123456")

    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"choices": [{"message": {"content": "Привет от Grok!"}}]})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = cm
    scm = MagicMock()
    scm.__aenter__ = AsyncMock(return_value=session)
    scm.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=scm):
        out = await ask_grok("Привет", user_name="Тест")

    assert out == "Привет от Grok!"
    called_url = session.post.call_args[0][0]
    assert called_url == "https://api.x.ai/v1/chat/completions"
    payload = session.post.call_args[1]["json"]
    assert payload["model"] == "grok-4.7"
    assert payload["max_tokens"] >= 1500
    assert session.post.call_args[1]["headers"]["Authorization"] == "Bearer xai-live-key-123456"


@pytest.mark.asyncio
async def test_ask_grok_model_fallback_on_404(monkeypatch):
    """При 404 на основной модели запрос повторяется со следующей моделью цепочки."""
    monkeypatch.setenv("GROK_API_KEY", "xai-live-key-123456")

    resp_404 = AsyncMock()
    resp_404.status = 404
    resp_404.text = AsyncMock(return_value='{"error":"model not found"}')
    cm_404 = MagicMock()
    cm_404.__aenter__ = AsyncMock(return_value=resp_404)
    cm_404.__aexit__ = AsyncMock(return_value=None)

    resp_200 = AsyncMock()
    resp_200.status = 200
    resp_200.json = AsyncMock(return_value={"choices": [{"message": {"content": "Ответ с резервной модели"}}]})
    cm_200 = MagicMock()
    cm_200.__aenter__ = AsyncMock(return_value=resp_200)
    cm_200.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.post.side_effect = [cm_404, cm_200]
    scm = MagicMock()
    scm.__aenter__ = AsyncMock(return_value=session)
    scm.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=scm):
        out = await ask_grok("Проверка", user_name="Тест")

    assert out == "Ответ с резервной модели"
    models_tried = [c[1]["json"]["model"] for c in session.post.call_args_list]
    assert models_tried[0] == "grok-4.7"
    assert models_tried[1] == "grok-4"


@pytest.mark.asyncio
async def test_ask_grok_empty_reply_uses_offline(monkeypatch):
    """Пустой ответ провайдера заменяется содержательным офлайн-ответом."""
    monkeypatch.setenv("GROK_API_KEY", "xai-live-key-123456")

    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post.return_value = cm
    scm = MagicMock()
    scm.__aenter__ = AsyncMock(return_value=session)
    scm.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=scm):
        out = await ask_grok("Какая столица Франции?", user_name="Тест")

    assert "Париж" in out


@pytest.mark.asyncio
async def test_ask_grok_no_key_offline():
    """Без ключа сразу используется офлайн-движок (без сетевого запроса)."""
    with patch("aiohttp.ClientSession") as session_cls:
        out = await ask_grok("Что такое фотосинтез?", user_name="Тест")
    session_cls.assert_not_called()
    assert "Фотосинтез" in out
