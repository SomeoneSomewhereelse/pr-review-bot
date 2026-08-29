FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY bot/pyproject.toml ./bot/pyproject.toml
RUN uv sync --frozen --no-dev --package bot

COPY bot ./bot
COPY dashboard ./dashboard

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "--no-dev", "uvicorn", "bot.main:app", "--host", "0.0.0.0", "--port", "8000"]
