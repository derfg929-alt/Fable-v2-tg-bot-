"""Сервис детального инвентаря игрока и продажи добычи (шахта, охота, рыбалка, предметы)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerMine, PlayerInventory
from app.services.mine_service import get_or_create_mine, ORE_PRICES
from app.services.games_service import HUNTING_PRICES, FISHING_PRICES
from app.services.economy_service import credit_coins


# Синонимы и сопоставление названий предметов для удобного ввода игроками
ITEM_NAME_ALIASES: dict[str, str] = {
    # Руда и слитки
    "уголь": "coal",
    "угля": "coal",
    "coal": "coal",
    "железо": "iron",
    "железа": "iron",
    "iron": "iron",
    "медь": "copper",
    "меди": "copper",
    "copper": "copper",
    "серебро": "silver",
    "серебра": "silver",
    "silver": "silver",
    "золото": "gold",
    "золота": "gold",
    "gold": "gold",
    "слиток": "processed_metals",
    "слитки": "processed_metals",
    "слитков": "processed_metals",
    "металл": "processed_metals",
    "металлы": "processed_metals",
    "processed_metals": "processed_metals",

    # Рыбалка
    "карась": "карась",
    "карася": "карась",
    "карасей": "карась",
    "щука": "щука",
    "щуку": "щука",
    "щуки": "щука",
    "щук": "щука",
    "золотая рыбка": "золотая рыбка",
    "рыбка": "золотая рыбка",
    "сундук с сокровищами": "сундук с сокровищами",
    "сундук": "сундук с сокровищами",
    "сундука": "сундук с сокровищами",
    "сундуки": "сундук с сокровищами",

    # Охота
    "заяц": "заяц",
    "зайца": "заяц",
    "зайцы": "заяц",
    "зайцев": "заяц",
    "олень": "олень",
    "оленя": "олень",
    "олени": "олень",
    "оленей": "олень",
    "медведь": "медведь",
    "медведя": "медведь",
    "медведи": "медведь",
    "медведей": "медведь",
    "дракон": "дракон",
    "дракона": "дракон",
    "драконы": "дракон",
    "драконов": "дракон",
}

ITEM_DISPLAY_INFO: dict[str, dict[str, str | int]] = {
    # Шахта
    "coal": {"icon": "🪨", "title": "Уголь", "price": ORE_PRICES["coal"], "source": "mine"},
    "iron": {"icon": "🔩", "title": "Железо", "price": ORE_PRICES["iron"], "source": "mine"},
    "copper": {"icon": "🥉", "title": "Медь", "price": ORE_PRICES["copper"], "source": "mine"},
    "silver": {"icon": "🥈", "title": "Серебро", "price": ORE_PRICES["silver"], "source": "mine"},
    "gold": {"icon": "🥇", "title": "Золото", "price": ORE_PRICES["gold"], "source": "mine"},
    "processed_metals": {"icon": "💎", "title": "Металлические слитки", "price": ORE_PRICES["processed_metals"], "source": "mine"},

    # Рыбалка
    "карась": {"icon": "🐟", "title": "Карась", "price": FISHING_PRICES["карась"], "source": "inventory"},
    "щука": {"icon": "🎣", "title": "Щука", "price": FISHING_PRICES["щука"], "source": "inventory"},
    "золотая рыбка": {"icon": "🐠", "title": "Золотая рыбка", "price": FISHING_PRICES["золотая рыбка"], "source": "inventory"},
    "сундук с сокровищами": {"icon": "👑", "title": "Древний сундук", "price": FISHING_PRICES["сундук с сокровищами"], "source": "inventory"},

    # Охота
    "заяц": {"icon": "🐇", "title": "Заяц", "price": HUNTING_PRICES["заяц"], "source": "inventory"},
    "олень": {"icon": "🦌", "title": "Олень", "price": HUNTING_PRICES["олень"], "source": "inventory"},
    "медведь": {"icon": "🐻", "title": "Медведь", "price": HUNTING_PRICES["медведь"], "source": "inventory"},
    "дракон": {"icon": "🐉", "title": "Дракон", "price": HUNTING_PRICES["дракон"], "source": "inventory"},
}


@dataclass
class InventoryItemEntry:
    key: str
    icon: str
    title: str
    quantity: int
    unit_price: int
    total_price: int
    category: str
    is_exclusive: bool = False
    can_sell: bool = True


@dataclass
class SellResult:
    item_title: str
    quantity: int
    unit_price: int
    total_earned: int
    remaining_quantity: int
    message: str


@dataclass
class SellAllResult:
    total_earned: int
    sold_items_count: int
    details: list[str]
    message: str


async def get_full_player_inventory(session: AsyncSession, *, user_id: int) -> dict:
    """Возвращает полную сводку инвентаря игрока со всеми категориями и стоимостью."""
    mine = await get_or_create_mine(session, user_id=user_id)
    inv_records = (
        await session.scalars(
            select(PlayerInventory).where(PlayerInventory.user_id == user_id).order_by(PlayerInventory.id)
        )
    ).all()

    mine_items: list[InventoryItemEntry] = []
    ore_fields = [
        ("coal", mine.coal),
        ("iron", mine.iron),
        ("copper", mine.copper),
        ("silver", mine.silver),
        ("gold", mine.gold),
        ("processed_metals", mine.processed_metals),
    ]
    for key, qty in ore_fields:
        if qty > 0:
            info = ITEM_DISPLAY_INFO[key]
            price = int(info["price"])
            mine_items.append(
                InventoryItemEntry(
                    key=key,
                    icon=str(info["icon"]),
                    title=str(info["title"]),
                    quantity=qty,
                    unit_price=price,
                    total_price=qty * price,
                    category="mine",
                    can_sell=True,
                )
            )

    farm_loot: list[InventoryItemEntry] = []
    exclusive_items: list[InventoryItemEntry] = []

    for item in inv_records:
        if item.quantity <= 0:
            continue
        item_key = item.item_id.lower()
        if item_key in ITEM_DISPLAY_INFO:
            info = ITEM_DISPLAY_INFO[item_key]
            price = int(info["price"])
            farm_loot.append(
                InventoryItemEntry(
                    key=item_key,
                    icon=str(info["icon"]),
                    title=str(info["title"]),
                    quantity=item.quantity,
                    unit_price=price,
                    total_price=item.quantity * price,
                    category=item.category,
                    can_sell=True,
                )
            )
        else:
            exclusive_items.append(
                InventoryItemEntry(
                    key=item.item_id,
                    icon="✨" if item.is_exclusive else "📦",
                    title=item.item_name,
                    quantity=item.quantity,
                    unit_price=0,
                    total_price=0,
                    category=item.category,
                    is_exclusive=item.is_exclusive,
                    can_sell=False,
                )
            )

    total_value = sum(i.total_price for i in mine_items) + sum(i.total_price for i in farm_loot)

    return {
        "mine": mine,
        "mine_items": mine_items,
        "farm_loot": farm_loot,
        "exclusive_items": exclusive_items,
        "total_value": total_value,
    }


def resolve_item_key(raw_input: str) -> str | None:
    """Распознает ключ предмета по пользовательскому названию."""
    clean = raw_input.strip().lower()
    if clean in ITEM_NAME_ALIASES:
        return ITEM_NAME_ALIASES[clean]
    # Поиск по началу или части слова
    for alias, key in ITEM_NAME_ALIASES.items():
        if clean == alias or clean.startswith(alias) or alias.startswith(clean):
            return key
    return None


async def sell_inventory_item(
    session: AsyncSession,
    *,
    user_id: int,
    item_query: str,
    quantity: int | None = 1,
) -> SellResult:
    """Продажа определенного предмета из инвентаря/шахты поштучно или в указанном количестве."""
    item_key = resolve_item_key(item_query)
    if not item_key or item_key not in ITEM_DISPLAY_INFO:
        valid_examples = ", ".join([v["title"] for v in list(ITEM_DISPLAY_INFO.values())[:7]])
        raise ValueError(
            f"Предмет «{item_query}» не найден или не подлежит продаже.\n"
            f"💡 Примеры доступных предметов: {valid_examples}."
        )

    info = ITEM_DISPLAY_INFO[item_key]
    unit_price = int(info["price"])
    item_title = str(info["title"])
    icon = str(info["icon"])

    # 1. Если это ресурс из шахты
    if info["source"] == "mine":
        mine = await get_or_create_mine(session, user_id=user_id)
        current_qty = getattr(mine, item_key, 0)
        if current_qty <= 0:
            raise ValueError(f"У вас нет предмета «{icon} {item_title}» для продажи!")

        to_sell = current_qty if (quantity is None or quantity >= current_qty) else max(1, quantity)
        setattr(mine, item_key, current_qty - to_sell)
        rem_qty = current_qty - to_sell
    else:
        # 2. Если это рыба или охотничий трофей
        inv_item = await session.scalar(
            select(PlayerInventory).where(
                PlayerInventory.user_id == user_id,
                PlayerInventory.item_id == item_key,
            ).with_for_update()
        )
        if not inv_item or inv_item.quantity <= 0:
            raise ValueError(f"У вас в инвентаре нет «{icon} {item_title}»!")

        to_sell = inv_item.quantity if (quantity is None or quantity >= inv_item.quantity) else max(1, quantity)
        inv_item.quantity -= to_sell
        rem_qty = inv_item.quantity
        if inv_item.quantity <= 0:
            await session.delete(inv_item)

    total_earned = to_sell * unit_price
    await credit_coins(
        session,
        user_id=user_id,
        amount=total_earned,
        reason=f"Продажа {item_title} ({to_sell} шт.)",
        operation_type="inventory_sell_item",
    )
    await session.flush()

    msg = (
        f"✅ <b>УСПЕШНАЯ ПРОДАЖА!</b>\n\n"
        f"{icon} Продано: <b>{to_sell} шт. «{item_title}»</b>\n"
        f"💵 Цена за штуку: <b>{unit_price:,}</b> монет\n"
        f"💰 Зачислено на баланс: <b>+{total_earned:,}</b> монет!\n"
        f"📦 Осталось в наличии: <b>{rem_qty}</b> шт.".replace(",", " ")
    )
    return SellResult(
        item_title=item_title,
        quantity=to_sell,
        unit_price=unit_price,
        total_earned=total_earned,
        remaining_quantity=rem_qty,
        message=msg,
    )


async def sell_all_inventory(session: AsyncSession, *, user_id: int) -> SellAllResult:
    """Мгновенно продает всю накопленную руду, слитки, улов рыбалки и трофеи охоты."""
    mine = await get_or_create_mine(session, user_id=user_id)
    inv_records = (
        await session.scalars(
            select(PlayerInventory).where(PlayerInventory.user_id == user_id).with_for_update()
        )
    ).all()

    total_earned = 0
    total_count = 0
    details: list[str] = []

    # Продажа руды
    ore_fields = ["coal", "iron", "copper", "silver", "gold", "processed_metals"]
    for field in ore_fields:
        qty = getattr(mine, field, 0)
        if qty > 0:
            info = ITEM_DISPLAY_INFO[field]
            earned = qty * int(info["price"])
            total_earned += earned
            total_count += qty
            details.append(f"• {info['icon']} {info['title']}: {qty} шт. (+{earned:,} монет)".replace(",", " "))
            setattr(mine, field, 0)

    # Продажа рыбалки и охоты
    for item in inv_records:
        key = item.item_id.lower()
        if key in ITEM_DISPLAY_INFO and item.quantity > 0:
            info = ITEM_DISPLAY_INFO[key]
            earned = item.quantity * int(info["price"])
            total_earned += earned
            total_count += item.quantity
            details.append(f"• {info['icon']} {info['title']}: {item.quantity} шт. (+{earned:,} монет)".replace(",", " "))
            await session.delete(item)

    if total_earned <= 0:
        raise ValueError("В вашем инвентаре нет ресурсов, рыбы или трофеев для продажи!")

    await credit_coins(
        session,
        user_id=user_id,
        amount=total_earned,
        reason="Продажа всей добычи из инвентаря",
        operation_type="inventory_sell_all",
    )
    await session.flush()

    msg = (
        f"💰 <b>ПОЛНАЯ ПРОДАЖА ДОБЫЧИ СКУПЩИКУ!</b>\n\n"
        f"Успешно реализовано <b>{total_count}</b> единиц товара:\n"
        + "\n".join(details)
        + f"\n\n💵 Всего получено: <b>+{total_earned:,}</b> монет в кошелек!".replace(",", " ")
    )
    return SellAllResult(
        total_earned=total_earned,
        sold_items_count=total_count,
        details=details,
        message=msg,
    )
