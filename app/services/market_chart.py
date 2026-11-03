"""Сервис генерации графиков биржи Fable.

Создает изображение графика цен, объема и спроса через matplotlib в байтах PNG
для непосредственной отправки в Telegram (без Web App / Mini App).
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketQuote, MarketTrade


async def generate_price_chart_bytes(
    session: AsyncSession, limit: int = 50
) -> bytes:
    """Генерирует график цен последних сделок/котировок."""
    trades = (
        await session.scalars(
            select(MarketTrade)
            .order_by(desc(MarketTrade.id))
            .limit(limit)
        )
    ).all()

    times: list[datetime] = []
    prices: list[float] = []
    volumes: list[int] = []

    if trades:
        for t in reversed(trades):
            times.append(t.created_at)
            prices.append(float(t.price))
            volumes.append(t.quantity)
    else:
        # Если сделок еще не было, берем котировки маркетмейкера
        quotes = (
            await session.scalars(
                select(MarketQuote)
                .order_by(desc(MarketQuote.id))
                .limit(limit)
            )
        ).all()
        for q in reversed(quotes):
            times.append(q.created_at)
            prices.append(float(q.price))
            volumes.append(q.volume)

    if not times:
        # Заглушка, если рынок только что инициализирован
        times = [datetime.now(timezone.utc)]
        prices = [1.0]
        volumes = [0]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(8, 5), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )
    fig.patch.set_facecolor("#1e1e2f")
    ax1.set_facecolor("#27293d")
    ax2.set_facecolor("#27293d")

    # График цены
    ax1.plot(times, prices, color="#00d2d3", linewidth=2, marker="o", markersize=4, label="Курс")
    ax1.set_title("Биржа Fable — График курса", color="white", fontsize=14, pad=10)
    ax1.set_ylabel("Цена (монеты)", color="white")
    ax1.tick_params(colors="white")
    ax1.grid(True, linestyle="--", alpha=0.3, color="#718093")
    ax1.legend(loc="upper left", facecolor="#1e1e2f", edgecolor="none", labelcolor="white")

    # Гистограмма объемов
    ax2.bar(times, volumes, width=0.001, color="#5f27cd", alpha=0.8, label="Объём")
    ax2.set_ylabel("Объём", color="white")
    ax2.tick_params(colors="white")
    ax2.grid(True, linestyle="--", alpha=0.3, color="#718093")

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=100, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
