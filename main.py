"""Точка входа для хостингов, которые запускают `python main.py`.

Основная точка входа проекта — `app.main` (см. README: `python -m app.main`).
Этот файл нужен платформам, которые по умолчанию ищут `main.py` в корне.
"""
from __future__ import annotations

import asyncio

from app.main import main

if __name__ == "__main__":
    asyncio.run(main())
