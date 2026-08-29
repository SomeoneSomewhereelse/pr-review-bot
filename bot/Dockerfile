FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
COPY bot/pyproject.toml ./bot/pyproject.toml
RUN uv sync --frozen --no-dev --package bot

COPY bot ./bot
# dashboard's code (and, critically, its pyproject.toml) must be physically
# present for uv run's workspace discovery to succeed at runtime -- even
# though this image never runs `uv sync --package dashboard`. Without this,
# `uv run` fails: "Workspace member /app/dashboard is missing a
# pyproject.toml", and the container crash-loops.
COPY dashboard ./dashboard

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# --no-sync is required because onboarding/ (a workspace member) is absent
# from this image; a plain `uv run` would try to re-sync the whole workspace
# and fail for the same missing-pyproject.toml reason as above.
CMD ["uv", "run", "--no-sync", "--no-dev", "uvicorn", "bot.main:app", "--host", "0.0.0.0", "--port", "8000"]
