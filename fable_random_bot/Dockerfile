# Сборка из корня репозитория: docker build -f fable_random_bot/Dockerfile -t fable-random-bot .
FROM python:3.12-slim

WORKDIR /app

COPY fable_random_bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY fable_random_bot ./fable_random_bot

# Папка /app/data сохраняется между перезапусками на Bothost.
ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

CMD ["python", "-m", "fable_random_bot.main"]
