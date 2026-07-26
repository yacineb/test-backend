# Contributing

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker with Compose v2, for the container workflow

You do **not** need a system Python. The project targets **Python 3.14 as the
minimum** (`requires-python = ">=3.14"`) and pins 3.14 in `.python-version`; uv
downloads that interpreter on first use.

## Run it

### With Docker (closest to production)

```bash
docker compose up --build
```

- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- Health: http://localhost:8000/health

Compose declares a healthcheck, so `docker compose ps` reports `healthy` once
the app answers. Stop with `docker compose down`.

### Locally, with hot reload

```bash
uv sync                                          # create .venv from uv.lock
uv run uvicorn app.main:app --reload             # http://127.0.0.1:8000
```

`uv sync` installs the dev group too. There is no `activate` step to remember —
`uv run <cmd>` executes inside the project environment.

## Tests and lint

```bash
uv run pytest                # test suite
uv run ruff check .          # lint
uv run ruff format .         # format
```

## Dependencies

`uv.lock` is committed and is the source of truth. Never hand-edit it.

```bash
uv add <package>             # runtime dependency
uv add --dev <package>       # dev-only dependency
uv lock --upgrade            # refresh the lock
```

The Docker build runs `uv sync --locked`, which **fails** if `uv.lock` is stale
relative to `pyproject.toml`. Commit both together.

## Layout

```
app/main.py        FastAPI app and the /health endpoint
tests/             pytest suite
Dockerfile         multi-stage build, distroless runtime
compose.yaml       single `api` service
pyproject.toml     deps, ruff and pytest config
```

`[tool.uv] package = false` — this is an application, not a library, so nothing
is built or installed as a package. `pytest` finds `app/` via
`pythonpath = ["."]`.

## About the Docker image

Two stages:

1. **builder** (`ghcr.io/astral-sh/uv`) — installs a relocatable CPython 3.14
   into `/opt/python` and the locked dependencies into `/app/.venv`. Deps are
   installed before the source is copied, so editing code does not invalidate
   the dependency layer.
2. **runtime** (`gcr.io/distroless/cc-debian12:nonroot`) — copies the
   interpreter, the venv and `app/`. No shell, no package manager, no
   `pip`/`uv`; runs as uid 65532.

The base is `cc` rather than distroless' `python3` because we ship our own
CPython 3.14 instead of inheriting whatever version that image currently
carries — that image lags well behind the current release. `cc` supplies the
glibc/libstdc++ that the interpreter and the compiled wheels (uvloop,
httptools, pydantic-core) link against.

Since there is no shell in the image, the Compose healthcheck cannot use
`curl` or a shell one-liner. It invokes the bundled interpreter directly in
exec form; keep that in mind if you change the probe.
