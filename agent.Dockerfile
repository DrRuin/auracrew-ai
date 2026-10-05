FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY criteria-agent/pyproject.toml criteria-agent/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY criteria-agent/app app
RUN uv sync --frozen --no-dev
EXPOSE 8080
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
