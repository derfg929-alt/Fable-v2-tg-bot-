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
from datetime import datetime
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.exc import StatementError

from app.config import get_settings
from app.db.session import session_scope
from app.db.models import Player
from app.services import contest_service as cs
from app.services import contest_settings as cset
from app.services import contest_referral_service as crefs
from app.services import leaderboard_service as lb
from app.services import referral_service as ds_ref
from app.services import draw_service as ds
from app.services import ticket_service as tickets
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


# Значения из URL/тела запроса (например, contest_id) могут не помещаться в
# целочисленный столбец БД. Без этого обработчика драйвер бросает OverflowError
# и клиент получает 500 вместо понятного «не найдено».
@app.exception_handler(OverflowError)
@app.exception_handler(StatementError)
async def _handle_bad_numeric_id(request: Request, exc: Exception):
    return JSONResponse(status_code=404, content={"ok": False, "message": "Конкурс не найден.", "reason": "not_found"})


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
        reserves = await ds.get_latest_reserves(session, contest_id)
        settings = cset.read_settings(contest)
        my_tickets = int(entry.tickets) if entry else 0
        invited = await lb.get_invited_count(session, contest_id, user_id)
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
            "settings": settings,
            "channels": [
                {"title": ch.title, "username": ch.username, "chat_id": ch.chat_id, "invite_link": ch.invite_link}
                for ch in channels
            ],
            "is_participant": bool(entry and entry.status == "active"),
            "my_tickets": my_tickets,
            "my_invited": invited,
            "winners": [
                {"place": w.place, "name": (p.custom_nickname or p.first_name) if p else f"ID {w.user_id}", "prize": w.prize}
                for w, p in winners
            ],
            "reserves": [
                {"place": w.place, "name": (p.custom_nickname or p.first_name) if p else f"ID {w.user_id}"}
                for w, p in reserves
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
    except cs.OwnerCannotJoinError:
        return JSONResponse(status_code=200, content={"ok": False, "message": "Ты организатор этого конкурса и не можешь в нём участвовать.", "reason": "owner"})
    except cs.BannedFromContestError:
        return JSONResponse(status_code=403, content={"ok": False, "message": "Вы заблокированы и не можете участвовать.", "reason": "banned"})
    except cs.ContestClosedError:
        return JSONResponse(status_code=200, content={"ok": False, "message": "Конкурс недоступен для участия.", "reason": "closed"})
    except cs.ContestNotFoundError:
        return JSONResponse(status_code=404, content={"ok": False, "message": "Конкурс не найден.", "reason": "not_found"})
    except cs.ContestError as e:
        return JSONResponse(status_code=200, content={"ok": False, "message": str(e), "reason": "contest_error"})
    except (OverflowError, StatementError):
        # contest_id не помещается в целочисленный столбец БД — такого конкурса нет.
        return JSONResponse(status_code=404, content={"ok": False, "message": "Конкурс не найден.", "reason": "not_found"})
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
            "/mycontests — мои конкурсы (для владельцев каналов)",
            "/createcontest — создать конкурс для своего канала",
        ],
    }


@app.get("/api/my-contests")
async def my_contests(x_init_data: str | None = Header(default=None)):
    """Конкурсы, созданные текущим пользователем как владельцем канала."""
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        contests = await cs.list_contests_by_owner(session, user_id)
        result = []
        for c in contests:
            stats = await cs.contest_statistics(session, c.id)
            result.append({
                "id": c.id,
                "title": c.title,
                "prize": c.prize,
                "status": cs.compute_status(c),
                "status_label": cs.STATUS_LABELS.get(cs.compute_status(c), c.status),
                "winners_count": c.winners_count,
                "participants": stats["participants_active"],
                "winners": stats["winners"],
                "ends_at": c.ends_at.isoformat() if c.ends_at else None,
                "peak_hour": stats["timeline"].get("peak_hour"),
            })
    return {"contests": result}


@app.get("/api/contests/{contest_id}/stats")
async def contest_stats(contest_id: int, x_init_data: str | None = Header(default=None)):
    """Полная статистика конкурса. Доступна владельцу конкурса или администратору."""
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            raise HTTPException(status_code=404, detail="Конкурс не найден")
        is_admin = await rset.is_admin(session, user_id)
        if contest.owner_id != user_id and not is_admin:
            raise HTTPException(status_code=403, detail="Статистика доступна только владельцу конкурса")
        stats = await cs.contest_statistics(session, contest_id)
        logs = await ds.get_draw_history(session, contest_id)
    stats["draws"] = [
        {"round": lg.round, "winners_count": lg.winners_count, "algorithm_version": lg.algorithm_version}
        for lg in logs
    ]
    return stats


# ===================== СОЗДАНИЕ КОНКУРСА (Mini App) =====================

class ContestCreateRequest(BaseModel):
    title: str
    description: str = ""
    prize: str | None = None
    image_url: str | None = None
    winners_count: int = 1
    ends_at: str | None = None
    settings: dict | None = None


class CodeWordRequest(BaseModel):
    contest_id: int
    word: str


@app.post("/api/contests/create")
async def create_contest(payload: ContestCreateRequest, x_init_data: str | None = Header(default=None)):
    """Создаёт конкурс без обязательного канала (участвовать смогут все)."""
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="Укажите название конкурса")

    ends_at = None
    if payload.ends_at:
        try:
            ends_at = datetime.fromisoformat(payload.ends_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректная дата окончания")

    async with session_scope() as session:
        contest = await cs.create_contest(
            session,
            title=payload.title,
            description=payload.description,
            image_url=payload.image_url,
            prize=payload.prize,
            winners_count=max(1, int(payload.winners_count)),
            ends_at=ends_at,
            status=cs.STATUS_ACTIVE,
            created_by=user_id,
            owner_id=user_id,
            settings=payload.settings,
        )
        contest_id = contest.id
        title = contest.title
    return {"ok": True, "contest_id": contest_id, "title": title}


@app.post("/api/contests/code-word")
async def submit_code_word(payload: CodeWordRequest, x_init_data: str | None = Header(default=None)):
    """Принимает кодовое слово и начисляет билеты."""
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        result = await tickets.redeem_code_word(
            session, contest_id=payload.contest_id, user_id=user_id, text=payload.word
        )
        if result.status == "ok":
            await crefs.mark_conditions_met(session, contest_id=payload.contest_id, user_id=user_id)
            await crefs.credit_referral_for_user(session, contest_id=payload.contest_id, user_id=user_id)
    messages = {
        "ok": f"Кодовое слово принято! +{result.tickets} билет(ов)",
        "already": "Ты уже вводил это кодовое слово",
        "disabled": "В этом конкурсе кодовые слова не предусмотрены",
        "no_entry": "Сначала нужно участвовать в конкурсе",
        "not_found": "Такое кодовое слово не найдено",
    }
    return {"ok": result.status == "ok", "status": result.status, "message": messages.get(result.status, "")}


@app.post("/api/contests/sponsor-task")
async def sponsor_task(payload: JoinRequest, x_init_data: str | None = Header(default=None)):
    """Зачитывает задание спонсора и начисляет билеты."""
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        awarded = await tickets.submit_sponsor_task(
            session, contest_id=payload.contest_id, user_id=user_id
        )
        if awarded:
            await crefs.mark_conditions_met(session, contest_id=payload.contest_id, user_id=user_id)
            await crefs.credit_referral_for_user(session, contest_id=payload.contest_id, user_id=user_id)
    return {"ok": awarded, "message": "Задание зачтено!" if awarded else "Ты уже выполнил это задание"}


@app.get("/api/contests/{contest_id}/participants")
async def contest_participants(
    contest_id: int,
    page: int = Query(default=1, ge=1),
    q: str | None = Query(default=None),
    x_init_data: str | None = Header(default=None),
):
    """Постраничный список участников (по 50) с поиском по username."""
    await _resolve_user(_require_init_data(x_init_data))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            raise HTTPException(status_code=404, detail="Конкурс не найден")
        settings = cset.read_settings(contest)
        if not settings.get("show_participants"):
            raise HTTPException(status_code=403, detail="Список участников скрыт организатором")
        offset = (page - 1) * 50
        rows = await cs.list_participants_public(session, contest_id, limit=50, offset=offset)
        total = await cs.participants_count(session, contest_id)
        items = []
        for entry, player, invited in rows:
            name = (player.custom_nickname or player.first_name) if player else f"ID {entry.user_id}"
            username = player.username if player else None
            if q and q.lstrip("@").lower() not in (username or "").lower() and q.lower() not in name.lower():
                continue
            item = {"name": name, "username": username, "invited": invited}
            if settings.get("show_tickets"):
                item["tickets"] = int(entry.tickets)
            items.append(item)
    return {"participants": items, "total": total, "page": page, "per_page": 50}


@app.get("/api/contests/{contest_id}/leaderboard")
async def contest_leaderboard(contest_id: int, x_init_data: str | None = Header(default=None)):
    """Публичный топ приглашающих конкурса (если включён)."""
    await _resolve_user(_require_init_data(x_init_data))
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            raise HTTPException(status_code=404, detail="Конкурс не найден")
        settings = cset.read_settings(contest)
        if not settings.get("leaderboard"):
            raise HTTPException(status_code=403, detail="Рейтинг в этом конкурсе не ведётся")
        top = await lb.get_top(session, contest_id, limit=int(settings.get("leaderboard_size", 10)))
    return {
        "leaderboard": [
            {
                "place": i,
                "name": (p.custom_nickname or p.first_name) if p else f"ID {uid}",
                "username": p.username if p else None,
                "invited": count,
            }
            for i, (uid, count, p) in enumerate(top, start=1)
        ]
    }


@app.get("/api/contests/{contest_id}/sources")
async def contest_sources(contest_id: int, x_init_data: str | None = Header(default=None)):
    """Отчёт по источникам приглашений (владелец конкурса или админ)."""
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            raise HTTPException(status_code=404, detail="Конкурс не найден")
        is_admin = await rset.is_admin(session, user_id)
        if contest.owner_id != user_id and not is_admin:
            raise HTTPException(status_code=403, detail="Отчёт доступен только организатору")
        report = await cs.sources_report(session, contest_id)
    return {"sources": report}


@app.get("/api/contests/{contest_id}/export")
async def contest_export(contest_id: int, x_init_data: str | None = Header(default=None)):
    """CSV-экспорт участников (владелец конкурса или админ)."""
    user = await _resolve_user(_require_init_data(x_init_data))
    user_id = user["id"]
    async with session_scope() as session:
        contest = await cs.get_contest(session, contest_id)
        if not contest:
            raise HTTPException(status_code=404, detail="Конкурс не найден")
        is_admin = await rset.is_admin(session, user_id)
        if contest.owner_id != user_id and not is_admin:
            raise HTTPException(status_code=403, detail="Экспорт доступен только организатору")
        rows = await cs.list_entries(session, contest_id, active_only=False)
        lines = ["user_id,username,name,tickets,status,invited_by,conditions_met"]
        for entry in rows:
            player = await session.get(Player, entry.user_id)
            inviter = await crefs.get_inviter(session, entry.user_id)
            username = (player.username if player else "") or ""
            name = (player.custom_nickname or player.first_name) if player else ""
            name = (name or "").replace(",", " ")
            lines.append(
                f"{entry.user_id},{username},{name},{int(entry.tickets or 0)},"
                f"{entry.status},{inviter or ''},{'yes' if entry.conditions_met else 'no'}"
            )
        csv_text = "\n".join(lines)
    return JSONResponse(content={"csv": csv_text, "filename": f"contest_{contest_id}.csv"})


@app.get("/api/contests/{contest_id}/verify")
async def contest_verify(contest_id: int, x_init_data: str | None = Header(default=None)):
    """Данные для проверки честности: участники, веса, seed, соль, хэш."""
    await _resolve_user(_require_init_data(x_init_data))
    async with session_scope() as session:
        logs = await ds.get_draw_history(session, contest_id)
    if not logs:
        return {"rounds": []}
    rounds = []
    for log in logs:
        weights = {str(k): int(v) for k, v in (log.weights or {}).items()}
        rounds.append({
            "round": log.round,
            "algorithm_version": log.algorithm_version,
            "seed": log.seed,
            "salt": log.salt,
            "participants_hash": log.participants_hash,
            "participants": [int(x) for x in (log.participants or [])],
            "weights": weights,
            "winners": [int(x) for x in (log.winners or [])],
        })
    return {"rounds": rounds}


@app.get("/verifier")
async def verifier_page():
    """Простая веб-страница для независимой проверки честности розыгрыша."""
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "verifier.html")
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return JSONResponse({"detail": "verifier.html не найден"}, status_code=404)


@app.get("/")
async def index():
    """Отдаёт интерфейс Mini App."""
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mini_app.html")
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return JSONResponse({"detail": "mini_app.html не найден"}, status_code=404)
