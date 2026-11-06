"""Сервис работы и подработки для Fable.

Механики:
- Основная государственная работа: доступна раз в 20 минут, доход зависит от уровня игрока (базово 150 + level * 50).
- Подработка (таксист, курьер, уборщик, бариста, грузчик):
  доступна раз в 10 минут, приносит умеренную сбалансированную прибыль (50 - 250 монет),
  а также опыт персонажа.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerJob
from app.services.economy_service import credit_coins, PlayerNotFoundError


class JobError(Exception):
    """Базовая ошибка работы."""


class JobCooldownError(JobError):
    """Кулдаун на работу еще не истек."""


@dataclass(frozen=True, slots=True)
class SideJobConfig:
    code: str
    title: str
    icon: str
    min_reward: int
    max_reward: int
    exp_reward: int
    action_texts: tuple[str, ...]


SIDE_JOBS: dict[str, SideJobConfig] = {
    "таксист": SideJobConfig(
        "таксист", "Таксист", "🚖", 80, 220, 15,
        (
            "Вы довезли пассажира до центра города без пробок!",
            "Успешный ночной заказ из аэропорта!",
            "Пассажир оставил щедрые чаевые за приятную музыку!",
        ),
    ),
    "курьер": SideJobConfig(
        "курьер", "Курьер", "🚴", 60, 180, 12,
        (
            "Вы быстро доставили горячую пиццу клиенту!",
            "Срочная доставка важных документов завершена вовремя!",
            "Заказ из супермаркета доставлен прямо до двери!",
        ),
    ),
    "уборщик": SideJobConfig(
        "уборщик", "Уборщик", "🧹", 50, 140, 10,
        (
            "Вы навели идеальный порядок в торговом зале!",
            "Офис бизнес-центра сияет чистотой!",
            "Вы тщательно убрали спортивный комплекс!",
        ),
    ),
    "бариста": SideJobConfig(
        "бариста", "Бариста", "☕️", 70, 190, 14,
        (
            "Вы сварили нежнейший авторский латте с карамелью!",
            "Клиенты выстроились в очередь за вашим капучино!",
            "Идеальная чашка эспрессо зарядила посетителей бодростью!",
        ),
    ),
    "грузчик": SideJobConfig(
        "грузчик", "Грузчик", "📦", 90, 240, 16,
        (
            "Вы успешно разгрузили фуру со строительными материалами!",
            "Партия техники бережно перенесена на склад!",
            "Смена на товарной станции завершена без происшествий!",
        ),
    ),
}

MAIN_JOB_COOLDOWN_SECONDS = 1200  # 20 минут
SIDE_JOB_COOLDOWN_SECONDS = 600   # 10 минут


async def get_or_create_player_job(session: AsyncSession, *, user_id: int) -> PlayerJob:
    job = await session.scalar(
        select(PlayerJob).where(PlayerJob.user_id == user_id).with_for_update()
    )
    if not job:
        job = PlayerJob(user_id=user_id, current_job="Стажёр")
        session.add(job)
        await session.flush()
    return job


async def perform_main_job(session: AsyncSession, *, user_id: int) -> tuple[int, int, str]:
    """Выполняет основную работу. Возвращает (награда_монеты, опыт, сообщение)."""
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    job = await get_or_create_player_job(session, user_id=user_id)
    now = datetime.now(timezone.utc)

    if job.last_worked_at:
        elapsed = (now - job.last_worked_at).total_seconds()
        if elapsed < MAIN_JOB_COOLDOWN_SECONDS:
            rem = int(MAIN_JOB_COOLDOWN_SECONDS - elapsed)
            mins = rem // 60
            secs = rem % 60
            raise JobCooldownError(f"Вы устали после смены! Следующая работа доступна через {mins} мин. {secs} сек.")

    # Баланс дохода основной работы
    reward = 200
    exp = 25

    await credit_coins(
        session,
        user_id=user_id,
        amount=reward,
        reason="Зарплата за смену на основной работе",
        operation_type="job_salary",
    )

    player.experience += exp
    job.last_worked_at = now
    await session.flush()

    msg = (
        f"💼 <b>РАБОЧАЯ СМЕНА ЗАВЕРШЕНА!</b>\n\n"
        f"Вы добросовестно отработали смену в должности <b>{job.current_job}</b>!\n"
        f"💰 Заработано: <b>+{reward:,}</b> монет\n"
        f"✨ Опыт: <b>+{exp}</b> XP".replace(",", " ")
    )
    return reward, exp, msg


from app.services.gear_service import get_or_create_gear, get_gear_bonus_percent


async def perform_side_job(session: AsyncSession, *, user_id: int, profession: str) -> tuple[int, int, str]:
    """Выполняет подработку.

    Награда рассчитывается случайно (случайный разброс) и увеличивается
    за счет соответствующего снаряжения/предмета игрока (рок-машина для такси,
    байк для курьера, кофемашина, экзоскелет, инвентарь уборщика).
    """
    prof_norm = profession.strip().lower()
    if prof_norm not in SIDE_JOBS:
        available = ", ".join(SIDE_JOBS.keys())
        raise JobError(f"Неизвестная подработка '{profession}'. Доступны: {available}")

    cfg = SIDE_JOBS[prof_norm]
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    job = await get_or_create_player_job(session, user_id=user_id)
    now = datetime.now(timezone.utc)

    if job.last_side_job_at:
        elapsed = (now - job.last_side_job_at).total_seconds()
        if elapsed < SIDE_JOB_COOLDOWN_SECONDS:
            rem = int(SIDE_JOB_COOLDOWN_SECONDS - elapsed)
            mins = rem // 60
            secs = rem % 60
            raise JobCooldownError(f"Вы пока отдыхаете после подработки! Следующий вызов через {mins} мин. {secs} сек.")

    # Получаем снаряжение игрока
    gear = await get_or_create_gear(session, user_id=user_id)
    gear_lvl = 1
    gear_name = ""

    if prof_norm == "таксист":
        gear_lvl = gear.taxi_car_level
        gear_name = f"🚕 Рок-машина (ур. {gear_lvl})"
    elif prof_norm == "курьер":
        gear_lvl = gear.courier_bike_level
        gear_name = f"🛵 Транспорт курьера (ур. {gear_lvl})"
    elif prof_norm == "бариста":
        gear_lvl = gear.barista_machine_level
        gear_name = f"☕️ Кофемашина (ур. {gear_lvl})"
    elif prof_norm == "грузчик":
        gear_lvl = gear.loader_exosuit_level
        gear_name = f"🦾 Экзоскелет (ур. {gear_lvl})"
    elif prof_norm == "уборщик":
        gear_lvl = gear.cleaner_gear_level
        gear_name = f"🧼 Инвентарь (ур. {gear_lvl})"

    # Случайная прибыль со сбалансированным распределением
    base_reward = random.randint(cfg.min_reward, cfg.max_reward)
    bonus_pct = get_gear_bonus_percent(gear_lvl)
    gear_bonus = int(base_reward * (bonus_pct / 100.0))
    total_reward = base_reward + gear_bonus

    exp = cfg.exp_reward + (gear_lvl - 1) * 2
    story = random.choice(cfg.action_texts)

    await credit_coins(
        session,
        user_id=user_id,
        amount=total_reward,
        reason=f"Подработка: {cfg.title} (снаряжение ур. {gear_lvl})",
        operation_type="job_side_reward",
    )

    player.experience += exp
    job.last_side_job_at = now
    await session.flush()

    bonus_line = f"⚡️ Бонус от снаряжения ({gear_name}): <b>+{gear_bonus}</b> монет (+{bonus_pct}%)\n" if gear_bonus > 0 else f"💡 <i>Прокачайте {gear_name} для роста дохода: <code>улучшить {prof_norm}</code>!</i>\n"

    msg = (
        f"{cfg.icon} <b>ПОДРАБОТКА: {cfg.title.upper()}</b>\n\n"
        f"{story}\n\n"
        f"💰 Оплата за заказ: <b>+{total_reward:,}</b> монет (база: {base_reward})\n"
        f"{bonus_line}"
        f"✨ Получено опыта: <b>+{exp}</b> XP".replace(",", " ")
    )
    return total_reward, exp, msg
