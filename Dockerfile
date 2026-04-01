FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bot_app ./bot_app
COPY tests ./tests
COPY run_bot.py run_web.py ./
COPY config ./config

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

CMD ["python", "run_web.py"]
