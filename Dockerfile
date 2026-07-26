# syntax=docker/dockerfile:1

# Build stage: uv installs a relocatable CPython 3.14 plus the locked deps.
FROM ghcr.io/astral-sh/uv:0.11.21-debian-slim AS builder

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON=3.14 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src

RUN uv python install 3.14

# Deps first: this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY app ./app

# Lean and secure runtime stage: distroless, no shell and no package manager.
FROM gcr.io/distroless/cc-debian12:nonroot AS runtime

COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /src/app /app/app

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

ENTRYPOINT ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", \
            "--host", "0.0.0.0", "--port", "8000"]
