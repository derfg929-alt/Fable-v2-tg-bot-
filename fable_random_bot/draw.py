"""Алгоритм жеребьёвки.

Пул формируется из участников: если у участника 5 билетов, его ID попадает в
пул 5 раз. Победители выбираются через secrets.choice (криптографический
рандом), выбранные удаляются из пула, чтобы не было дублей. Публикуется
SHA-256 хэш от списка участников с числом билетов и соль.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any


def build_pool(contest: dict[str, Any], users: dict[str, Any]) -> list[str]:
    """Список слотов: ID участника повторяется столько раз, сколько у него билетов."""
    pool: list[str] = []
    for participant in contest.get("participants", []):
        tickets = int(users.get(participant, {}).get("contest_tickets", {}).get(
            str(contest.get("id")), 0
        ))
        # У участника как минимум 1 базовый билет.
        tickets = max(1, tickets)
        pool.extend([participant] * tickets)
    return pool


def transparency_payload(contest: dict[str, Any], users: dict[str, Any], salt: str) -> dict[str, Any]:
    lines = []
    for participant in sorted(contest.get("participants", []), key=lambda x: int(x)):
        tickets = max(
            1,
            int(users.get(participant, {}).get("contest_tickets", {}).get(str(contest.get("id")), 0)),
        )
        lines.append(f"{participant}:{tickets}")
    digest_source = "|".join(lines) + "|" + salt
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
    return {
        "salt": salt,
        "sha256": digest,
        "total_tickets": sum(
            max(
                1,
                int(users.get(p, {}).get("contest_tickets", {}).get(str(contest.get("id")), 0)),
            )
            for p in contest.get("participants", [])
        ),
        "participants": len(contest.get("participants", [])),
    }


def draw_winners(
    contest: dict[str, Any],
    users: dict[str, Any],
    winners_count: int,
    reserve_count: int,
    rng: secrets.SystemRandom | None = None,
) -> dict[str, Any]:
    """Выбрать победителей и запасных. Возвращает dict с winners/reserve/transparency."""
    rng = rng or secrets.SystemRandom()
    pool = build_pool(contest, users)
    salt = secrets.token_hex(16)

    winners: list[str] = []
    reserve: list[str] = []

    total_needed = winners_count + reserve_count
    for _ in range(total_needed):
        if not pool:
            break
        chosen = rng.choice(pool)
        # Удаляем выбранного из пула целиком — дубли невозможны.
        pool = [slot for slot in pool if slot != chosen]
        if len(winners) < winners_count:
            winners.append(chosen)
        else:
            reserve.append(chosen)

    return {
        "winners": winners,
        "reserve": reserve,
        "transparency": transparency_payload(contest, users, salt),
    }
