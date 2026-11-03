import pytest
import asyncio
from app.db.session import session_scope, engine
from sqlalchemy import text

@pytest.fixture(autouse=True)
async def cleanup_db():
    yield
    async with session_scope() as session:
        # Очищаем данные между тестами
        await session.execute(text("TRUNCATE TABLE players, chats, market_orders, market_trades, game_sessions, bank_interest_runs, moderation_cases, chat_admins, chat_rules, chat_link_whitelist, captcha_challenges, admin_action_logs CASCADE;"))
