"""Тесты устойчивости запуска (порт хостинга, сигналы) и полноты Mini App.

Проверяем:
- серверный API слушает порт $PORT хостинга, если он задан (иначе процесс
  убивают по SIGTERM, и Mini App становится недоступным);
- публичный адрес Mini App берётся из MINI_APP_URL/PUBLIC_URL;
- uvicorn не перехватывает сигналы (иначе конфликт с aiogram и «SIGTERM»);
- в интерфейсе Mini App есть кнопки управления конкурсом (создание, розыгрыш,
  завершение, отмена, экспорт) — «половина функций» не должна теряться.
"""
from __future__ import annotations

import os
import re

import pytest

from app.config import Settings


def _settings(**env):
    base = {
        "BOT_TOKEN": "123456789:ABCdefGhIJKLMNoPQRsTUVwxyZ",
        "OWNER_ID": "1",
    }
    base.update(env)
    # _env_file=None — игнорируем локальный .env, чтобы тест не зависел от окружения.
    return Settings(_env_file=None, **base)


def test_effective_port_prefers_platform_port():
    s = _settings(API_PORT="8080", PORT="3000")
    assert s.effective_api_port == 3000


def test_effective_port_falls_back_to_api_port():
    s = _settings(API_PORT="8080")
    assert s.effective_api_port == 8080


def test_effective_mini_app_url_prefers_mini_app_url():
    s = _settings(MINI_APP_URL="https://example.com/mini", PUBLIC_URL="https://pub.example.com")
    assert s.effective_mini_app_url == "https://example.com/mini"


def test_effective_mini_app_url_uses_public_url_and_adds_scheme():
    s = _settings(PUBLIC_URL="app.example.com")
    assert s.effective_mini_app_url == "https://app.example.com"


def test_effective_mini_app_url_from_render_env(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://fable.onrender.com")
    s = _settings()
    assert s.effective_mini_app_url == "https://fable.onrender.com"


@pytest.mark.asyncio
async def test_api_server_binds_platform_port(monkeypatch):
    """_maybe_start_api использует $PORT и не даёт uvicorn перехватывать сигналы."""
    import app.main as main

    seen = {}

    class FakeServer:
        def __init__(self, config):
            seen["port"] = config.port
            seen["host"] = config.host
            self.should_exit = False

        async def serve(self, *a, **k):
            return None

    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(main.get_settings(), "port", 3000, raising=False)
    monkeypatch.setattr(main.get_settings(), "api_port", 8080, raising=False)

    settings = main.get_settings()
    result = await main._maybe_start_api(settings)
    assert seen["port"] == 3000
    server, task = result
    # uvicorn не должен захватывать SIGTERM/SIGINT — иначе процесс падает.
    import contextlib

    assert server.capture_signals is contextlib.nullcontext
    task.cancel()


def test_mini_app_has_owner_management_buttons():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
    html = open(path, encoding="utf-8").read()
    for needle in (
        "openManage(",
        "renderCreateForm(",
        "runDraw(",
        "finishContest(",
        "cancelContest(",
        "archiveContest(",
        "deleteContest(",
        "exportContest(",
        "openVerify(",
        "/api/contests/\" + id + \"/settings",
    ):
        assert needle in html, f"В Mini App отсутствует «{needle}»"


def test_mini_app_has_create_button_on_home():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
    html = open(path, encoding="utf-8").read()
    assert re.search(r'onclick="renderCreateForm\(\)"', html), "Нет кнопки создания конкурса"


def test_index_route_serves_index_html():
    """Эндпойнт `/` отдаёт index.html, а не устаревший mini_app.html."""
    import app.api as api

    response = api._serve_static_html("index.html", "mini_app.html")
    assert response.status_code == 200
    assert str(response.path).endswith("index.html")


@pytest.mark.asyncio
async def test_main_setup_bot_menu_registers_webapp(monkeypatch):
    """Кнопка меню Mini App регистрируется, если задан публичный адрес."""
    import app.main as main
    from aiogram.types import MenuButtonWebApp

    captured = {}

    class FakeBot:
        async def set_chat_menu_button(self, menu_button=None):
            captured["menu"] = menu_button

    settings = _settings(MINI_APP_URL="https://example.com/")
    await main._setup_bot_menu(FakeBot(), settings)
    assert isinstance(captured["menu"], MenuButtonWebApp)
    assert captured["menu"].web_app.url == "https://example.com/"


@pytest.mark.asyncio
async def test_main_setup_bot_menu_skips_without_url():
    import app.main as main

    class FakeBot:
        async def set_chat_menu_button(self, menu_button=None):  # pragma: no cover
            raise AssertionError("не должен вызываться без URL")

    settings = _settings()
    await main._setup_bot_menu(FakeBot(), settings)
