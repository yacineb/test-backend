# Logging and correlation

Structured JSON on stdout, one line per meaningful event, with enough
correlation to follow a single upload from the HTTP request through four
pipeline steps to the partner webhook that finishes it.

## Application code uses the standard library

```python
logger = logging.getLogger(__name__)
logger.info("upload.stored", extra={"document_id": ..., "size_bytes": ...})
```

No layer imports structlog. It is configured once in `app/observability.py` as
the *renderer*, and stdlib records pass through it. That keeps `domain` and
`application` importing nothing but the standard library, exactly as the
hexagonal layering requires, and means swapping the renderer never touches a
call site.

Event names are dotted, stable identifiers rather than sentences, so they can be
grepped, counted and alerted on without parsing prose. Everything variable goes
in `extra`, never interpolated into the name.

## Correlation

Three keys, because one is not enough once work outlives the request:

| key | covers | set by |
|---|---|---|
| `request_id` | one HTTP request | `RequestContextMiddleware`, returned in `X-Request-Id` |
| `org_id`, `user_id` | everything after authentication | the auth dependency |
| `document_id` | one document, including detached workers | the call sites that have it |
| `workflow_id` | one pipeline run | `pipeline.enqueued` ties it to the request |

An inbound `X-Request-Id` is honoured so a gateway can correlate across
services, but it is sanitised first: it is attacker-controlled and lands in
every log line for the request, which is the classic log-injection path. Values
outside `[A-Za-z0-9._-]{1,64}` are replaced, not escaped.

**`request_id` does not reach every pipeline line, and that is expected.** DBOS
runs the parallel `metadata` and `chunking` branches as queued workflows on
their own tasks, so the request's contextvars do not propagate into them. This
is why `document_id` is on every pipeline line: it is the key that survives the
hand-off. `pipeline.enqueued` records `request_id` and `workflow_id` together,
which is the join between the two halves.

## Levels

The point of a level is to answer "does anyone need to look at this?".

| level | means | examples |
|---|---|---|
| `ERROR` | we failed at something we promised | `upload.row_failed`, `pipeline.failed`, any 5xx |
| `WARNING` | a request was refused, worth watching in aggregate | `request.rejected`, `auth.token.rejected`, `webhook.partner.unverified`, `pipeline.step.attempt_failed` |
| `INFO` | durable state changed | `upload.stored`, `pipeline.step.succeeded`, `auth.login.succeeded` |
| `DEBUG` | mechanics; off in production | `storage.put.aborted` |

Two deliberate calls:

- **Client errors are WARNING, not ERROR.** A user uploading a PNG is the API
  working. Paging on it is how alerting gets ignored. A *spike* in 415s or 401s
  is a different story, which is what aggregation is for.
- **A failed pipeline attempt is WARNING, not ERROR.** The provider mocks fail a
  third of the time by design and the orchestrator retries. Only exhausting the
  retries — `pipeline.failed` — is a real failure.

Health probes log at DEBUG. At INFO they would be the majority of the volume
while saying nothing.

## Never logged

Passwords, access and refresh tokens, HMAC signatures, file contents. The
correlation keys above are enough to follow any request without them.
`tests/api/test_logging.py` asserts this against the rendered output, not just
the log records — a token that only appears after formatting would still be a
token in a log file.

`upload.stored` records a 12-character `sha256_prefix` rather than the full
digest: enough to correlate two uploads of the same bytes or check an object
against its row, without duplicating the identifier wholesale.

## Two things this configuration deliberately prevents

**Application log level must not switch on SQL echo.** `sqlalchemy.engine`
emits every statement *and its bound parameters* as soon as its effective level
reaches INFO — which, inheriting an INFO root, meant thousands of lines per
request containing filenames, digests and the email being looked up on the auth
path. Third-party loggers are pinned to WARNING; whether to echo SQL is
`DB_ECHO`'s decision. At `LOG_LEVEL=DEBUG` the operator has asked for the
firehose and gets it.

**Alembic must not disable the application's loggers.** The stock Alembic
template calls `fileConfig(...)` with `disable_existing_loggers=True`, which
sets `.disabled` on every logger the ini does not name — all of `app.*`
included. Anything running a migration in-process then logs nothing at all,
silently, without a single level changing. `migrations/env.py` passes
`disable_existing_loggers=False`.

Both are covered by `tests/unit/test_observability.py`.

## Configuration

| variable | default | meaning |
|---|---|---|
| `LOG_LEVEL` | `INFO` | root level |
| `LOG_FORMAT` | `json` | `console` for a readable local run |

uvicorn runs with `--no-access-log`: `RequestContextMiddleware` emits the access
line itself, with the request id, tenant and duration attached. Leaving
uvicorn's on would log every request twice, and its copy is the one without the
correlation fields.

## What is not here

No OpenTelemetry. Tracing spans are worth having when there is a collector to
send them to and more than one service to join across; today there is neither,
and an exporter pointed at nothing is configuration without a payoff. The
correlation keys above already answer "what happened to this upload" from
`docker compose logs`. The trigger for revisiting is a second service in the
request path — at that point `request_id` propagation becomes a trace, and the
`X-Request-Id` handling here is the seam it plugs into.
