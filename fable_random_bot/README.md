# Fable Random Bot (JSON)

Telegram-бот для розыгрышей на каналах. Управление и участие — только через
кнопки и текстовые сообщения. **Без** Mini App, FastAPI/Uvicorn, HTTPS и
GitHub Pages: один Python-процесс (aiogram 3) с polling.

База данных — один JSON-файл. На хостинге это `/app/data/database.json` (папка
`/app/data` сохраняется между перезапусками и деплоями), локально —
`fable_random_bot/data/database.json`. Запись атомарная (`.tmp` + `os.replace`),
все операции «прочитать → изменить → записать» идут под глобальным
`asyncio.Lock`.

## Структура

```
fable_random_bot/
├── main.py              # Точка входа: Bot, Dispatcher, APScheduler, polling
├── config.py            # BOT_TOKEN, ADMIN_IDS, путь к БД
├── database.py          # Атомарное JSON-хранилище + CRUD
├── keyboards.py         # Reply- и Inline-клавиатуры
├── draw.py              # Взвешенный выбор победителей + прозрачность (SHA-256, соль)
├── admin_flow.py        # Пошаговый мастер создания конкурса (состояние в JSON)
├── utils.py             # Реф-ссылки, очередь проверок подписки
├── handlers/
│   ├── start.py         # /start, меню, /top, /participants
│   ├── admin.py         # Мастер создания конкурса
│   ├── user.py          # Участие, рефералы, кодовые слова, «Поделиться»
│   └── draw.py          # Жеребьёвка, итоги, передача приза запасным
├── requirements.txt
├── Dockerfile
└── data/                # Создаётся автоматически
```

## Механики

- **Билеты за рефералов**: реферал засчитывается только за реальное действие
  приглашённого (участие с выполненными условиями). Это не фильтр ботов, а
  логика «реферал = действие».
- **Кодовые слова**: за ввод слова начисляются билеты; повторный ввод не считается.
- **Задания спонсоров**: текст задания выдаётся участнику.
- **Публичный список участников** (`/participants`) и **рейтинг приглашающих**
  (`/top`) — постранично, при включении в настройках конкурса.
- **Кнопка «Поделиться»** — готовый текст с реферальной ссылкой.
- **Запасные победители**: если основной не откликнулся за
  `reserve_timeout_hours`, приз переходит к первому запасному.
- **Прозрачность**: после розыгрыша публикуются общее число слотов, SHA-256
  хэш списка участников с билетами и соль.

## Переменные окружения

| Переменная | Описание |
| --- | --- |
| `BOT_TOKEN` | Токен от @BotFather (обязательно) |
| `ADMIN_IDS` | Telegram ID админов через запятую, напр. `111,222` |
| `DATABASE_PATH` | Путь к JSON-файлу (по умолчанию `/app/data/database.json`) |
| `TIMEZONE` | Часовой пояс планировщика (по умолчанию `Europe/Moscow`) |

## Запуск

```bash
pip install -r fable_random_bot/requirements.txt
export BOT_TOKEN="123456:ABC..."
export ADMIN_IDS="111222333"
python -m fable_random_bot.main
```

## Деплой на Bothost

1. Загрузи репозиторий на GitHub (ветка `main`).
2. В Bothost создай бота, укажи репозиторий и главный файл `fable_random_bot/main.py`.
3. В переменных окружения задай `BOT_TOKEN` (и `ADMIN_IDS`).
4. Путь к базе — `/app/data/database.json` (папка `/app/data` сохраняется).
5. Задеплой и напиши боту `/start`.
6. Создай тестовый конкурс, перезапусти бота и убедись, что конкурс остался в базе.

Docker-сборка из корня репозитория:

```bash
docker build -f fable_random_bot/Dockerfile -t fable-random-bot .
docker run -e BOT_TOKEN=... -e ADMIN_IDS=... -v fable_data:/app/data fable-random-bot
```

## Тесты

```bash
python -m pytest tests/test_random_bot_json.py -q
```
