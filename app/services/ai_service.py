"""Сервис искусственного интеллекта для бота Fable.

- Откликается на слово 'фабле' / 'fable'.
- Поддерживает диалог, запоминает историю сообщений чата/пользователя.
- Умеет решать математические выражения (например: 6÷7*8÷99*68).
- Если в .env передан API ключ (AI_API_KEY / OPENAI_API_KEY), выполняет запрос к нейросети, сохраняя контекст диалога.
- Если ключа нет, использует встроенный мощный алгоритм ответов и математического вычислителя.
"""
from __future__ import annotations

import re
import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AIConversation


def solve_math_expression(text: str) -> str | None:
    """Пытается безопасно вычислить математическое выражение в тексте."""
    cleaned = text.replace("÷", "/").replace("×", "*").replace(":", "/").replace(",", ".").strip()
    match = re.search(r"([\d\.\s\+\-\*\/\(\)\^]{3,})", cleaned)
    if not match:
        return None
    expr = match.group(1).strip()
    if not any(c in expr for c in "+-*/"):
        return None
    # Безопасная проверка символов
    if not re.match(r"^[\d\.\s\+\-\*\/\(\)]+$", expr):
        return None
    try:
        # Безопасный расчет
        val = eval(expr, {"__builtins__": {}}, {})
        if isinstance(val, (int, float)):
            if isinstance(val, float):
                return f"{val:.6g}"
            return str(val)
    except Exception:
        return None
    return None


async def get_ai_reply(session: AsyncSession, *, chat_id: int, user_id: int, prompt: str) -> str:
    """Генерирует ответ ИИ с учетом истории диалога."""
    cfg = get_settings()
    api_key = cfg.ai_api_key or cfg.openai_api_key

    # Сначала проверяем, не математический ли это пример
    math_res = solve_math_expression(prompt)

    # Загружаем последние 8 реплик для контекста
    history_rows = (
        await session.scalars(
            select(AIConversation)
            .where(AIConversation.chat_id == chat_id)
            .order_by(AIConversation.created_at.desc())
            .limit(8)
        )
    ).all()
    history = list(reversed(history_rows))

    # Сохраняем вопрос пользователя
    session.add(AIConversation(user_id=user_id, chat_id=chat_id, role="user", content=prompt))

    answer = None

    if api_key:
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты — умный и дружелюбный виртуальный помощник Fable (Фабле) в Telegram-боте. "
                    "Отвечай кратко, емко, интересно, с юмором и эмодзи. Помогай с вопросами, расчетами и общением."
                ),
            }
        ]
        for h in history:
            messages.append({"role": h.role, "content": h.content})
        messages.append({"role": "user", "content": prompt})

        try:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": cfg.ai_model,
                "messages": messages,
                "max_tokens": 500,
                "temperature": 0.7,
            }
            url = f"{cfg.ai_base_url.rstrip('/')}/chat/completions"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http_sess:
                async with http_sess.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            answer = choices[0].get("message", {}).get("content", "").strip()
        except Exception:
            pass

    # Если API недоступен или ключа нет
    if not answer:
        if math_res is not None:
            answer = f"🧮 Результат вычисления: <b>{math_res}</b>"
        else:
            lower_p = prompt.lower()
            if any(w in lower_p for w in ["привет", "хай", "здравствуй"]):
                answer = "Привет! Я Фабле, твой верный ИИ-помощник. Чем могу помочь?"
            elif any(w in lower_p for w in ["как дела", "как ты"]):
                answer = "Всё отлично! Готов к вашим вопросам, играм и вычислениям! ✨"
            elif any(w in lower_p for w in ["кто ты", "что ты такое"]):
                answer = "Я Фабле — искусственный интеллект и хранитель этого чата! Могу считать примеры, отвечать на вопросы и запоминать наш разговор."
            else:
                answer = f"Я услышал тебя: «<i>{prompt}</i>». Задавай вопросы, пиши примеры — я помогу!"

    session.add(AIConversation(user_id=user_id, chat_id=chat_id, role="assistant", content=answer))
    await session.flush()
    return answer
