# Project memory — Fable v2 Telegram bot

Index of durable, non-obvious facts learned while working in this repo.

## Testing
- Tests run against in-memory SQLite (`tests/conftest.py` sets `DATABASE_URL=sqlite+aiosqlite:///:memory:`); the app itself targets PostgreSQL. `app/db/session.py` builds the async URL from whatever scheme is configured.
- Full suite: `python -m pytest -q` (~120 tests, ~12s warm). First run after a fresh env can be much slower (cold imports); re-run before blaming a change.
- New test files must `import pytest` and use `@pytest.mark.asyncio`; `pytest.ini` sets `asyncio_mode=auto`.

## Casino / games (`app/services/games_service.py`)
- All gambling games are tuned to **~90% RTP** (house edge ~10%). Payout multipliers (2.0x, x5, x15, 2.5 …) are kept stable on purpose so UI/tests don't break; only probabilities / payout coefficients change.
- To change a game's edge, adjust: slots `random.choices` weights, dice bot-reroll bias, coinflip win prob, mystery-box weights, crash win prob, shooting target probs, roulette payout multiplier. Verify with a pure-python Monte-Carlo replica — running the real async DB path for millions of rounds is far too slow.
- `play_slots(dice_value=...)` decodes Telegram dice (1..64) and must keep returning fixed results (x15 jackpot at 64, x5 triples) — asserted in `tests/test_slots_and_admin_fixes.py`.

## AI (`app/services/ai_service.py`, `ai_knowledge.py`)
- Provider priority: xAI Grok (`GROK_API_KEY`) then Groq (`GROQ_API_KEY`); no key → offline engine `build_offline_response`. Network/timeout/provider failures should fall back to the offline engine, not raw error strings.
- In handlers use `bot.id` (parsed locally from the token) instead of `await bot.get_me()` to avoid a network round-trip per message.

## Gotchas
- `select` must be imported at module level in handlers (`from sqlalchemy import func, select`). `handle_balance` in `economy_handlers.py` previously crashed with `NameError` because it was only imported inside a different function.
- Root-level legacy files (`Config.py`, `Models.py`, `Bank`, `Economy`, `Activity`, `Reputation`, `Transfer`, `Acievement`, `History`, `Player_servise.py`) are dead code — nothing imports them; real code lives under `app/`. `Player_servise.py` is not even valid Python.

## AI memory & engagement (2026-11)
- AI dialogue memory lives in `app/services/ai_memory_service.py` (model `AIConversation`, table `ai_conversations`, unique on `(user_id, chat_id)`). History is per-user **and per-chat**; handler passes `message.chat.id`, so in DMs that equals the user id. `ai_handlers.handle_ai_query` loads history, calls `ask_grok(..., chat_history=...)`, then `remember_exchange` in a separate `session_scope`. "забудь" clears it.
- `build_offline_response(prompt, user_name, context)` in `ai_knowledge.py` accepts a `context` string ("Игрок: …\nFable: …") so follow-ups ("а подробнее?") stay on topic via `_is_followup` / `_last_user_topic`.
- Background engagement worker (`app/tasks/background.py`) and owner broadcast are both rate-limited to **once per 24h**. Owner broadcast state is stored in `GlobalSetting` key `owner_broadcast_state` (not a dedicated table).

## Owner panel / broadcast
- Owner admin panel command: "понель"/"панель" (typo is intentional and is the primary trigger). DM-only (`ChatType.PRIVATE` from `aiogram.enums`); in groups it returns so it doesn't shadow group admin. Routers are registered owner-first in `app/main.py`.
- Owner broadcast command is `рассылка [текст]` (also a panel button `owner_menu_broadcast`); enforced via `get_broadcast_cooldown` / `mark_broadcast_sent` in `owner_service.py`.

## Money exploits fixed
- `launch_rocket_mission` previously granted coins with **no cost/cooldown** (infinite money). Now has `LAUNCH_COOLDOWN_SECONDS=1800`, recorded as a `GameSession(game_type="cosmodrome_launch")`.
- `bait:claim` callback credited 25 coins with no idempotency → now uses `idempotency_key=f"bait:{chat_id}:{message_id}"` so one drop = one claim.
- `market_maker.run_market_maker_cycle` cancelled its own orders each cycle without returning reserves → now refunds `coin_reserve`/`asset_reserve` before re-quoting (else liquidity drained to zero).

## Global ban
- `Player.is_globally_banned` is enforced in `MessageActivityAndSecurityMiddleware` (blocks text/caption commands; private-chat notice). Does not affect callbacks or chat-level admin.

## Per-chat (local) ban
- Chat bans are **per-chat**, stored as `ModerationCase(action="ban")` keyed by `(chat_id, target_id)`; there is no per-chat boolean on `Player`. Enforced in **both** middlewares: `MessageActivityAndSecurityMiddleware` deletes the banned user's messages in that chat, and `CallbackAnswerMiddleware` blocks their inline-button presses there (`_blocked_by_local_ban`). Admins (level ≥2) are exempt.
- Helpers live in `moderation_service.py`: `has_active_chat_ban(session, chat_id, target_id)` and `revoke_ban(...)` (level-3 gate). The `разбан` handler calls `revoke_ban`; before that fix the local ban case was never deactivated (only Telegram unban was called).
- Local bans must never touch `Player.is_globally_banned`, never delete player data (wallet/businesses/mining), and never propagate to other chats. Verified by `tests/test_chat_local_ban.py`.
- Test seeding gotcha: to make a moderator level-3 in a test chat, create the `Chat` row first (`get_or_create_chat`), then insert `ChatAdmin(chat_id, user_id, level=3, is_active=True)`; FK to `chats.telegram_id` otherwise fails silently on SQLite flush.

## Gotchas (more)
- `aiogram.types` has **no** `ChatType`; import from `aiogram.enums`. This caused real crashes in `owner_handlers` and `games_handlers`.
- `get_or_create_player` requires keyword-only `first_name` — `owner_handlers.handle_give_coins` crashed without it.
- `EconomyResult` has `transaction/actor_balance/recipient_balance/duplicate` — **no** `current_balance`.
- Bank interest in code is 1%/0.5%/0.2%/0.1% (docs/README/ai_knowledge previously claimed 5%/3%/1.5%/0.5% — now corrected).
- Tests that pass a `MagicMock` as the middleware `event` must use `spec=types.Message`, otherwise `isinstance(event, types.Message)` is False and the middleware is skipped.
