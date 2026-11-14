"""Серверный API Fable Randomizer для Telegram Mini App.

Безопасность:
- Все запросы требуют валидный Telegram initData, подписанный ботом.
- Telegram ID пользователя берётся ТОЛЬКО из проверенного initData;
  значение из браузера не принимается на веру.
- Проверяется срок жизни подписи (auth_date).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import get_settings
from app.db.session import session_scope
from app.db.models import Player
from app.services import contest_service as cs
from app.services import referral_service as ds_ref
from app.services import draw_service as ds
from app.services import randomizer_settings as rset
from app.services.player_service import get_or_create_player

logger = logging.getLogger("fable.api")

app = FastAPI(title="Fable Randomizer API", version="1.0")

# Mini App может открываться с любого origin внутри Telegram.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===================== ПРОВЕРКА initData =====================

class InitDataError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, ttl_seconds: int = 86400) -> dict:
    """Проверяет подпись Telegram initData и возвращает разобранные данные.

    Алгоритм из документации Telegram Web Apps:
      secret_key = HMAC_SHA256(bot_token, "WebAppData")
      hash = HMAC_SHA256(data_check_string, secret_key)
    """
    if not init_data:
        raise InitDataError("Пустые данные initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("В initData отсутствует hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        raise InitDataError("Неверная подпись initData")

    if ttl_seconds and ttl_seconds > 0:
        try:
            auth_date = int(pairs.get("auth_date", "0"))
        except ValueError:
            raise InitDataError("Некорректный auth_date")
        if auth_date <= 0 or (time.time() - auth_date) > ttl_seconds:
            raise InitDataError("Данные initData устарели")

    user_json = pairs.get("user")
    if not user_json:
        raise InitDataError("В initData нет данных пользователя")
    try:
        user = json.loads(user_json)
    except json.JSONDecodeError:
        raise InitDataError("Некорректный JSON пользователя")

    return {"user": user, "auth_date": pairs.get("auth_date"), "raw": pairs}


async def _resolve_user(init_data: str) -> dict:
    """Проверяет initData и гарантирует регистрацию пользователя. Возвращает профиль."""
    settings = get_settings()
    try:
        parsed = validate_init_data(init_data, settings.bot_token_value, settings.initdata_ttl_seconds)
    except InitDataError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user = parsed["user"]
    user_id = user.get("id")
    if not isinstance(user_id, int) or user_id <= 0:
        raise HTTPException(status_code=401, detail="Некорректный ID пользователя")

    async with session_scope() as session:
        await get_or_create_player(
            session,
            telegram_id=user_id,
            first_name=user.get("first_name") or "Игрок",
            last_name=user.get("last_name"),
            username=user.get("username"),
        )
    return user


def _require_init_data(x_init_data: str | None) -> str:
    if not x_init_data:
        raise HTTPException(status_code=401, detail="Отсутствует заголовок X-Init-Data")
    return x_init_data


# ===================== МОДЕЛИ ОТВЕТОВ =====================

class JoinRequest(BaseModel):
    contest_id: int


class JoinResponse(BaseModel):
    ok: bool
    message: str


# ===================== ЭНДПОИНТЫ =====================

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/me")
async def me(x_init_data: str | None = Header(default=None)):
    init = _require_init_data(x_init_data)
    user = await _resolve_user(init)
    user_id = user["id"]
    async with session_scope() as session:
        player = await session.get(Player, user_id)
        invited = await ds_ref.count_invited(session, user_id)
        stats = await cs.get_user_stats(session, user_id)
        bot_username = await rset.get_bot_username(session)
    ref_link = ds_ref.build_referral_link(bot_username, user_id) if bot_username else None
    return {
        "user": {
            "id": user_id,
            "first_name": user.get("first_name"),
            "username": user.get("username"),
            "registered_at": player.registered_at.isoformat() if player and player.registered_at else None,
            "is_banned": bool(player.is_globally_banned) if player else False,
        },
        "stats": stats,
        "invited": invited,
        "referral_link": ref_link,
    }


@app.get("/api/contests")
async def list_contests(
    category: str = Query(default="active"),
    x_init_data: str | None = Header(default=None),
):
    await _resolve_user(_require_init_data(x_init_data))
    async with session_scope() as session:
        contests = await cs.list_contests(session, category=category)
        result = []
        for c in contests:
            result.append({
                "id": c.id,
                "title": c.title,
                "description": c.description,
                "image_url": c.image_url,
                "prize": c.prize,
                "status": cs.compute_status(c),
                "status_label": cs.STATUS_LABELS.get(cs.compute_status(c), c.status),
                "winners_count": c.winners_count,
                "participants": await cs.participants_count(session, c.id),
                "starts_at": c.starts_at.isoformat() if c.starts_at else None,
                "ends_at": c.ends_at.isoformat() if c.ends_at else None,
            })
    return {"contests": result}


@app.get("/api/contests/{contest_id}")
async def contest_detail(contest_id: int, x_init_data: str | None = Header(default=None)):
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            raise HTTPException(status_code=404, detail="Конкурс не найден")
        channels = await cs.get_contest_channels(session, contest_id)
        entry = await cs.get_entry(session, contest_id, user_id)
        winners = await ds.get_latest_winners(session, contest_id)
        return {
            "id": contest.id,
            "title": contest.title,
            "description": contest.description,
            "image_url": contest.image_url,
            "prize": contest.prize,
            "status": cs.compute_status(contest),
            "status_label": cs.STATUS_LABELS.get(cs.compute_status(contest), contest.status),
            "winners_count": contest.winners_count,
            "participants": await cs.participants_count(session, contest_id),
            "starts_at": contest.starts_at.isoformat() if contest.starts_at else None,
            "ends_at": contest.ends_at.isoformat() if contest.ends_at else None,
            "extra_conditions": contest.extra_conditions,
            "allow_leave": contest.allow_leave,
            "channels": [
                {"title": ch.title, "username": ch.username, "chat_id": ch.chat_id, "invite_link": ch.invite_link}
                for ch in channels
            ],
            "is_participant": bool(entry and entry.status == "active"),
            "winners": [
                {"place": w.place, "name": (p.custom_nickname or p.first_name) if p else f"ID {w.user_id}", "prize": w.prize}
                for w, p in winners
            ],
        }


@app.post("/api/contests/join", response_model=JoinResponse)
async def join_contest(payload: JoinRequest, x_init_data: str | None = Header(default=None)):
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    settings = get_settings()
    bot = Bot(token=settings.bot_token_value, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        async with session_scope() as session:
            bonus = await ds_ref.get_referral_bonus_units(session, user_id)
            await cs.join_contest(
                session,
                bot,
                contest_id=payload.contest_id,
                user_id=user_id,
                first_name=user.get("first_name") or "Игрок",
                username=user.get("username"),
                bonus_units=bonus,
            )
        return JoinResponse(ok=True, message="Ты успешно зарегистрирован в конкурсе! 🎉")
    except cs.NotSubscribedError:
        return JSONResponse(status_code=200, content={"ok": False, "message": "Для участия необходимо подписаться на все указанные каналы", "reason": "not_subscribed"})
    except cs.AlreadyParticipatingError:
        return JSONResponse(status_code=200, content={"ok": False, "message": "Ты уже участвуешь в этом конкурсе!", "reason": "already"})
    except cs.BannedFromContestError:
        return JSONResponse(status_code=403, content={"ok": False, "message": "Вы заблокированы и не можете участвовать.", "reason": "banned"})
    except cs.ContestClosedError:
        return JSONResponse(status_code=200, content={"ok": False, "message": "Конкурс недоступен для участия.", "reason": "closed"})
    except Exception as e:  # noqa: BLE001
        logger.exception("Ошибка участия через API: %s", e)
        return JSONResponse(status_code=500, content={"ok": False, "message": "Ошибка сервера"})
    finally:
        await bot.session.close()


@app.post("/api/contests/leave", response_model=JoinResponse)
async def leave_contest(payload: JoinRequest, x_init_data: str | None = Header(default=None)):
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        try:
            ok = await cs.leave_contest(session, contest_id=payload.contest_id, user_id=user_id)
        except cs.ContestClosedError:
            return JSONResponse(status_code=200, content={"ok": False, "message": "После завершения конкурса отмена участия невозможна."})
        except cs.ContestError as e:
            return JSONResponse(status_code=200, content={"ok": False, "message": str(e)})
    if ok:
        return JoinResponse(ok=True, message="Ты вышел из конкурса.")
    return JSONResponse(status_code=200, content={"ok": False, "message": "Ты не участвуешь в этом конкурсе."})


@app.get("/api/history")
async def history(filter: str = Query(default="all"), x_init_data: str | None = Header(default=None)):
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        items = await cs.get_user_entries(session, user_id, filter_=filter)
        result = []
        for entry, contest in items:
            winner = await cs.is_winner(session, contest.id, user_id)
            result.append({
                "contest_id": contest.id,
                "title": contest.title,
                "prize": contest.prize,
                "joined_at": entry.joined_at.isoformat() if entry.joined_at else None,
                "status": entry.status,
                "contest_status": cs.compute_status(contest),
                "is_winner": winner,
            })
    return {"history": result}


@app.get("/api/winners")
async def winners(x_init_data: str | None = Header(default=None)):
    await _resolve_user(_require_init_data(x_init_data))
    async with session_scope() as session:
        contests = await cs.list_contests(session, category="all", include_archived=True)
        result = []
        for c in contests:
            wlist = await ds.get_latest_winners(session, c.id)
            if not wlist:
                continue
            drawn_at = wlist[0][0].drawn_at if wlist[0][0].drawn_at else None
            result.append({
                "contest_id": c.id,
                "title": c.title,
                "prize": c.prize,
                "participants": await cs.participants_count(session, c.id),
                "drawn_at": drawn_at.isoformat() if drawn_at else None,
                "winners": [
                    {"place": w.place, "name": (p.custom_nickname or p.first_name) if p else f"ID {w.user_id}", "prize": w.prize}
                    for w, p in wlist
                ],
            })
    return {"contests": result}


@app.get("/api/help")
async def help_info(x_init_data: str | None = Header(default=None)):
    await _resolve_user(_require_init_data(x_init_data))
    async with session_scope() as session:
        support = await rset.get_support_link(session)
    return {
        "support_link": support,
        "commands": [
            "/contests — список конкурсов",
            "/join ID — участие в конкурсе",
            "/myref — реферальная ссылка",
            "/profile — профиль",
            "/history — история участия",
            "/winners — победители",
        ],
    }


@app.get("/")
async def index():
    """Отдаёт интерфейс Mini App."""
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mini_app.html")
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return JSONResponse({"detail": "mini_app.html не найден"}, status_code=404)
