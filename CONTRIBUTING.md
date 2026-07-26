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
the app answers. Stop with `docker compose down` (add `-v` to drop the database
volume too).

Boot order is `db` → `migrate` → `seed` → `api`, each gated on the previous one
finishing, so the API never starts against an unmigrated database.

### Signing in

The seed creates two organizations with one user each:

| Organization | Email | Password |
|---|---|---|
| Acme Corp | `alice@acme.example.com` | `password123` |
| Globex | `bob@globex.example.com` | `password123` |

`POST /auth/login` returns an access token; paste it into the **Authorize**
button in Swagger to exercise the authenticated endpoints.

### Locally, with hot reload

```bash
uv sync                                          # create .venv from uv.lock
docker compose up -d db                          # Postgres on its own
uv run alembic upgrade head                      # schema, roles, RLS policies
uv run python -m app.seed                        # two orgs, two users
uv run uvicorn app.main:app --reload             # http://127.0.0.1:8000
```

The compose `db` service does not publish a port, so for a host-side run either
add one or point `DATABASE_URL` / `AUTH_DATABASE_URL` / `MIGRATION_DATABASE_URL`
at your own Postgres. `uv sync` installs the dev group too. There is no
`activate` step to remember — `uv run <cmd>` executes inside the project
environment.

## Tests and lint

```bash
uv run pytest                # unit + API tests, no database needed
uv run ruff check .          # lint
uv run ruff format .         # format
```

Unit and API tests use in-memory fakes for the repositories, so the default run
needs no services. The integration tests exercise row-level security against a
real Postgres and are **skipped** unless you point them at one:

```bash
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=appdb --name pg-test postgres:17-alpine
TEST_POSTGRES_DSN='postgres:postgres@localhost:5433/appdb' uv run pytest
```

The fixture drops and recreates the `public` schema, then runs the real Alembic
migration — so the policies and roles are under test, not a reimplementation of
them. Point it only at a throwaway database.

## Authentication and tenancy

Access tokens are JWTs (`JWT_ACCESS_TTL_SECONDS`, 6h default). Refresh tokens
are opaque, stored as a SHA-256 hash, and rotated on every use; replaying a
consumed one revokes its whole family. Auth is a FastAPI dependency rather than
middleware, so it stays off `/health` and shows up in the OpenAPI schema.

Tenant isolation is enforced twice. Repositories filter on `org_id`, and
Postgres row-level security keys off `app.current_org_id`, which the request
session sets per transaction. The application connects as `app_rw`, which is
subject to those policies; a separate `app_auth` role holds `BYPASSRLS` and is
used only for lookups that precede authentication — user by email, refresh
token by hash — plus seeding.

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
app/domain/          entities, ports (Protocols), errors — no I/O, no framework
app/application/     use cases: login, refresh, logout
app/infrastructure/  SQLAlchemy, argon2, PyJWT — the adapters
app/api/             FastAPI routers, dependencies, error mapping
app/config.py        settings, all env-driven
app/seed.py          idempotent development seed
app/main.py          app factory and the /health endpoint
migrations/          Alembic; 0001 creates the schema, roles and RLS policies
tests/unit/          use cases against in-memory fakes
tests/api/           routes with the adapters overridden
tests/integration/   real Postgres; proves RLS. Skipped without TEST_POSTGRES_DSN
Dockerfile           multi-stage build, distroless runtime
compose.yaml         db, migrate, seed, api
pyproject.toml       deps, ruff and pytest config
```

Dependencies point inward only: `domain` imports nothing but the standard
library, `application` imports `domain`, `infrastructure` implements the
domain's ports, and `api` wires them together.

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
