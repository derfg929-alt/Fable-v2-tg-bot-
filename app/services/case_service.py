"""Сервис кейсов для Fable.

10 видов кейсов:
1. Деревянный кейс (цена: 500 монет)
2. Бронзовый кейс (цена: 2 000 монет)
3. Серебряный кейс (цена: 5 000 монет)
4. Золотой кейс (цена: 15 000 монет)
5. Платиновый кейс (цена: 50 000 монет)
6. Алмазный кейс (цена: 150 000 монет)
7. Рубиновый кейс (цена: 500 000 монет)
8. Изумрудный кейс (цена: 1 500 000 монет)
9. Мифический кейс (цена: 5 000 000 монет)
10. Галактический кейс (цена: 20 000 000 монет)

Возможный дроп:
- Монеты (от мелких до джекпота)
- Очки рейтинга
- Майнинг-риги и суперкомпьютеры
- Бизнесы (включая дорогие)
- Недвижимость (квартиры, виллы, небоскребы)
- Эксклюзивные коллекционные предметы (падают только из кейсов)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerBusiness, PlayerMining, PlayerProperty, PlayerInventory
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError
from app.services.mining_service import get_or_create_mining, get_rigs_dict, MINING_RIGS
from app.services.business_service import BUSINESSES


class CaseError(Exception):
    """Базовая ошибка кейсов."""


@dataclass(frozen=True, slots=True)
class CaseConfig:
    id: int
    name: str
    icon: str
    price: int
    description: str


CASES: dict[int, CaseConfig] = {
    1: CaseConfig(1, "Деревянный кейс", "📦", 500, "Базовый кейс для начинающих"),
    2: CaseConfig(2, "Бронзовый кейс", "🥉", 2_000, "Хороший старт и полезные ресурсы"),
    3: CaseConfig(3, "Серебряный кейс", "🥈", 5_000, "Шанс на фермы и рейтинговые очки"),
    4: CaseConfig(4, "Золотой кейс", "🥇", 15_000, "Ценные награды и стартовые бизнесы"),
    5: CaseConfig(5, "Платиновый кейс", "💠", 50_000, "Мощные риги и солидные выплаты"),
    6: CaseConfig(6, "Алмазный кейс", "💎", 150_000, "Высокий шанс на недвижимость и бизнес"),
    7: CaseConfig(7, "Рубиновый кейс", "🔴", 500_000, "Элитный кейс с ASIC-майнерами"),
    8: CaseConfig(8, "Изумрудный кейс", "🟢", 1_500_000, "Роскошная недвижимость и IT-бизнесы"),
    9: CaseConfig(9, "Мифический кейс", "🔮", 5_000_000, "Квантовые майнеры и эксклюзивы"),
    10: CaseConfig(10, "Галактический кейс", "🌌", 20_000_000, "Максимальный куш, небоскребы и нефтяной холдинг"),
}

EXCLUSIVE_ITEMS: list[tuple[str, str, str]] = [
    ("ancient_coin", "Древняя монета Сатоши", "artifact"),
    ("cyber_implant", "Кибернетический нейрочип", "cyber"),
    ("golden_crown", "Корона Императора Fable", "luxury"),
    ("quantum_core", "Квантовое ядро сингулярности", "scifi"),
    ("dragon_egg", "Яйцо Золотого Дракона", "mythic"),
]


@dataclass(frozen=True, slots=True)
class CaseOpenResult:
    case_name: str
    reward_type: str  # coins, rating, rig, business, property, exclusive
    reward_title: str
    message: str


async def open_case(session: AsyncSession, *, user_id: int, case_id: int) -> CaseOpenResult:
    """Открытие кейса с генерацией дропа."""
    if case_id not in CASES:
        raise CaseError(f"Кейс #{case_id} не найден. Выберите номер от 1 до 10.")

    cfg = CASES[case_id]
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cfg.price:
        raise InsufficientFundsError(f"Недостаточно монет для открытия: требуется {cfg.price:,} монет, у вас {player.wallet:,}".replace(",", " "))

    await debit_coins(
        session,
        user_id=user_id,
        amount=cfg.price,
        reason=f"Открытие кейса #{case_id} ({cfg.name})",
        operation_type="case_buy",
    )

    roll = random.random()
    msg_lines = [f"{cfg.icon} <b>ОТКРЫТИЕ: {cfg.name.upper()}</b>\n"]

    # 1. Шанс на бизнес (для дорогих кейсов от 4 до 10)
    if case_id >= 4 and roll < (0.05 + case_id * 0.015):
        # Выбираем бизнес по силе кейса
        max_b = min(10, case_id)
        min_b = max(1, case_id - 3)
        b_id = random.randint(min_b, max_b)
        b_cfg = BUSINESSES[b_id]

        existing = await session.scalar(
            select(PlayerBusiness).where(PlayerBusiness.user_id == user_id, PlayerBusiness.business_id == b_id)
        )
        if not existing:
            pb = PlayerBusiness(user_id=user_id, business_id=b_id, level=1, last_collected_at=datetime.now(timezone.utc))
            session.add(pb)
            msg_lines.append(f"🎉 <b>ДЖЕКПОТ! ВАМ ВЫПАЛ БИЗНЕС!</b>\n🏢 <b>{b_cfg.name}</b> {b_cfg.icon} добавлен в вашу империю!")
            reward_type = "business"
            reward_title = b_cfg.name
        else:
            # Компенсация монетами
            comp = int(b_cfg.base_cost * 0.5)
            await credit_coins(session, user_id=user_id, amount=comp, reason=f"Повтор бизнеса #{b_id} из кейса", operation_type="case_reward")
            msg_lines.append(f"🏢 Выпал бизнес <b>{b_cfg.name}</b>, но он уже есть! Вы получили компенсацию: <b>+{comp:,}</b> монет!".replace(",", " "))
            reward_type = "coins"
            reward_title = f"{comp} монет"

    # 2. Шанс на майнинг-оборудование (риги)
    elif roll < 0.25:
        mining = await get_or_create_mining(session, user_id=user_id)
        rig_keys = ["cpu"]
        if case_id >= 3:
            rig_keys.append("gpu")
        if case_id >= 6:
            rig_keys.append("asic")
        if case_id >= 8:
            rig_keys.append("quantum")

        rig_type = random.choice(rig_keys)
        r_cfg = MINING_RIGS[rig_type]
        rigs = get_rigs_dict(mining)
        rigs[rig_type] = rigs.get(rig_type, 0) + 1
        mining.rigs_json = rigs
        mining.computers = sum(rigs.values())

        msg_lines.append(f"⚡️ <b>ДОБЫЧА ОБОРУДОВАНИЯ!</b>\n{r_cfg.icon} <b>{r_cfg.name}</b> установлен на вашу ферму!\nМощность рига: +{r_cfg.hashrate} GH/s")
        reward_type = "rig"
        reward_title = r_cfg.name

    # 3. Шанс на эксклюзивный предмет (только из кейсов!)
    elif case_id >= 5 and roll < 0.35:
        item_id, item_title, cat = random.choice(EXCLUSIVE_ITEMS)
        inv = await session.scalar(
            select(PlayerInventory).where(PlayerInventory.user_id == user_id, PlayerInventory.item_id == item_id)
        )
        if not inv:
            inv = PlayerInventory(user_id=user_id, item_id=item_id, item_name=item_title, category=cat, quantity=1, is_exclusive=True)
            session.add(inv)
        else:
            inv.quantity += 1
        msg_lines.append(f"✨ <b>ЭКСКЛЮЗИВНЫЙ ДРОП!</b>\n🏆 Вы выбили уникальный коллекционный артефакт: <b>«{item_title}»</b>!\nОн добавлен в ваш инвентарь и украшает профиль!")
        reward_type = "exclusive"
        reward_title = item_title

    # 4. Шанс на рейтинг
    elif roll < 0.60:
        pts = int(cfg.price // 50 * random.uniform(0.8, 1.8))
        pts = max(5, pts)
        player.rating += pts
        msg_lines.append(f"⭐ <b>ВЛИЯНИЕ И АВТОРИТЕТ!</b>\nВам начислено: <b>+{pts:,} очков рейтинга</b>!".replace(",", " "))
        reward_type = "rating"
        reward_title = f"{pts} рейтинга"

    # 5. Выплата монет
    else:
        mult = random.choice([0.4, 0.7, 1.2, 1.6, 2.5, 4.0])
        payout = int(cfg.price * mult)
        if payout > 0:
            await credit_coins(session, user_id=user_id, amount=payout, reason=f"Дроп из кейса #{case_id}", operation_type="case_reward")
        msg_lines.append(f"💰 <b>МОНЕТЫ!</b>\nВам выпало: <b>+{payout:,} монет</b> (x{mult:.1f})!".replace(",", " "))
        reward_type = "coins"
        reward_title = f"{payout} монет"

    await session.flush()
    return CaseOpenResult(
        case_name=cfg.name,
        reward_type=reward_type,
        reward_title=reward_title,
        message="\n".join(msg_lines),
    )
