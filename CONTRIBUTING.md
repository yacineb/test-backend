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

## The document pipeline

A completed upload starts a four-step pipeline — OCR, then metadata and
chunking in parallel, then an outbound call to a partner — orchestrated by
[DBOS Transact](https://github.com/dbos-inc/dbos-transact-py) as durable,
checkpointed steps. `POST /documents` returns 201 as soon as the row is
committed; follow progress with `GET /documents/{id}`.

The pipeline stops at `awaiting_partner`. Only a verified `POST
/webhooks/partner` moves a document to `ready`.

DBOS keeps its state in the `dbos` schema of the same database and migrates it
on launch, so compose gains no service. Worker writes go through the same
RLS-scoped sessions as request writes; a worker's organization comes from its
workflow argument.

**`app/pipeline/steps.py` is reproduced byte-for-byte from `README.md` and must
not be edited** — not even reformatted. `tests/unit/test_steps_contract.py`
diffs it against the statement, and the file is excluded from both `ruff format`
and its import sorting in `pyproject.toml`.

- [docs/pipeline.md](docs/pipeline.md) — data model, retry policy, the webhook
  contract, testing
- [docs/architecture.md](docs/architecture.md) — why DBOS, and what would
  change the answer at 100k documents/day

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
app/application/     use cases: login, refresh, logout, upload_document
app/infrastructure/  SQLAlchemy, argon2, PyJWT, POSIX object store — the adapters
app/api/             FastAPI routers, dependencies, error mapping
app/pipeline/        DBOS steps and workflow; steps.py is the statement verbatim, body-size guard
app/config.py        settings, all env-driven
app/seed.py          idempotent development seed
app/main.py          app factory and the /health endpoint
scripts/             simulate_pipeline.py — where the latency numbers come from
migrations/          Alembic; 0001 schema/roles/RLS; 0002 documents; 0003 pipeline; 0004 listing index
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

## Security headers

`SecurityHeadersMiddleware` (`app/api/security_headers.py`) applies the OWASP
header set to every response, including ones produced by other middleware. The
values come from [`secure`](https://github.com/TypeError/secure)'s `STRICT`
preset rather than being written by hand, so they track that library rather than
drifting with us.

Only the Content-Security-Policy is chosen locally, keyed on the response's
`Content-Type` rather than on a path list:

- **JSON** (everything the API returns) gets `default-src 'none'` — a JSON body
  renders nothing, so it should be able to load nothing.
- **HTML** (`/docs`, `/redoc`) gets a policy permitting the jsdelivr CDN, Google
  Fonts and the favicon host that FastAPI's docs pages load from, and drops
  `Cross-Origin-Embedder-Policy`, whose `require-corp` value would block those
  CDN assets.

`tests/api/test_security_headers.py` parses the real docs HTML and asserts every
external asset it references is permitted by the CSP actually served — a strict
CSP otherwise blanks out Swagger while still returning `200`.

The `Server` banner is suppressed by uvicorn's `--no-server-header` in the
Dockerfile, not by the middleware: uvicorn appends its banner after the ASGI app
has returned, so setting it in application code produces two `Server` headers.

## Document uploads

`POST /documents` streams a multipart upload into an `ObjectStore` and records a
row; `GET /documents` lists the caller's organization. Both take the organization
and the uploader from the bearer token — neither is a request parameter.

Only PDFs are accepted, decided by sniffing the file's leading bytes with
`puremagic` — never by the `Content-Type` the client sent, which is a claim
rather than evidence. A PNG renamed `report.pdf` and declared `application/pdf`
gets a `415`. The sniffed type is what gets recorded, so a genuine PDF uploaded
as `application/octet-stream` is stored as `application/pdf`.

The listing is newest-first and paged by cursor, not offset: pass the previous
response's `next_cursor` back as `?cursor=`, and stop when it comes back null.
Each row carries the document's name, id, processing status, import date, and
the user who imported it (id, name, email, from a join to `users`).

| variable | default | meaning |
|---|---|---|
| `STORAGE_ROOT` | `/data/uploads` | where the POSIX backend writes; a compose volume |
| `MAX_UPLOAD_BYTES` | `104857600` | per-file limit, 100 MiB |
| `MAX_BODY_OVERHEAD_BYTES` | `1048576` | slack above it for multipart framing |
| `UPLOAD_CHUNK_BYTES` | `1048576` | read/write chunk size |

For a host-side run, point `STORAGE_ROOT` somewhere writable (`STORAGE_ROOT=./var/uploads`).

The rationale — the storage port's atomicity contract, why the size limit is
enforced in two places, and when to move to presigned S3 uploads — is in
[docs/upload-architecture.md](docs/upload-architecture.md). The read side —
why keyset paging, what is in the cursor, the measured cost against 2M rows,
and why GraphQL is the better long-term shape — is in
[docs/document-listing.md](docs/document-listing.md).

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
