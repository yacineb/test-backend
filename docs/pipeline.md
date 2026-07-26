# The document processing pipeline

How the pipeline is built and why. The choice of orchestrator is argued
separately in [architecture.md](architecture.md); this is about the code that
is here.

```
upload committed ──► ocr ──► metadata ──┐
                        └──► chunking ──┴──► external_call ──► awaiting_partner
                                                                      │
                                              POST /webhooks/partner ─┘──► ready
```

## What triggers it

The pipeline is triggered as the upload finishes — specifically, when the
transaction holding the document row and its four step rows **commits**. Only
then is the job enqueued.

That order is the whole point. Enqueueing inside the transaction is a race the
worker usually wins, because it is not waiting on an HTTP client: it would pick
up a job and find nothing to update.

`upload_document` in `app/application/upload_document.py` owns this. It already
owned "what makes an upload acceptable"; it now also owns "the upload is
finished", which is the same fact. It commits, starts the pipeline, records the
workflow id, and commits again.

That requires a session the handler commits itself, so the upload endpoint runs
on `Database.tenant_session_manual` rather than the request-scoped
`tenant_session`, which only commits when its block ends — after the response.
Committing clears `app.current_org_id`, since `set_config` asks for a
transaction-local value so it cannot leak to the next checkout of a pooled
connection; `TenantUnitOfWork` re-pins it after each commit, keeping that
property intact.

`tests/unit/test_upload_document.py` asserts a commit had already happened when
the runner was called, and that a failed upload never starts a pipeline.

## What `external_call` is for

The partner is a **regulated-archive service**: it indexes the chunks into a
compliance vault, validates the extracted metadata against retention rules, and
publishes the document to the client's records system. The work takes minutes
to hours on their side, so the call returns only an opaque `job_id` and the
outcome arrives later by webhook.

## The webhook seam

The workflow **ends** at `awaiting_partner`. It does not park on `DBOS.recv()`.
Parking works, but holding workflow state open for hours against a third party
buys nothing a status column and a correlation key do not, and splitting at
that seam keeps the inbound webhook independently testable.

The contract, which `DbPartnerJobSink` relies on:

- A document is **never** in `awaiting_partner` without a visible
  `partner_job_id`. Both are written in the transaction that marks
  `external_call` succeeded, because the partner may call back the instant it
  returns.
- `partner_job_id` is unique, so a notification can never resolve to two
  documents.
- Resolution runs on the **system session** (`BYPASSRLS`). The partner names no
  tenant — `job_id` is all it has — so there is nothing to scope the lookup to
  until the row is found. The organization is then *derived* from that row.
  This is the same reasoning that already puts "find a user by email" on the
  system session during login.
- Delivery is idempotent. Partners retry, and a document that has left
  `awaiting_partner` is decided; re-applying could flip a `ready` document to
  `failed` on a stale retry.

`ready` is reachable only through this path.

## Following progress

`GET /documents/{id}/events` streams `text/event-stream`. Measured against the
running stack, a status change reaches a connected client in **~80ms** — the
target was "of the order of a second".

The mechanism is one hop, because the event source already exists: every status
change is a write to `document_steps`, and migration `0004` turns every such
write into a `NOTIFY`. Postgres is already the broker, so nothing was added.

```
worker / webhook ──commit──► trigger ──pg_notify('document_progress', <doc_id>)
                                                    │
                                     one LISTEN connection per API process
                                                    │
                                          in-memory fan-out
                                                    │
                                  SSE task re-reads projection → yields
```

**Why a trigger rather than application code.** `NOTIFY` is transactional: it
is delivered on commit and never for a write that rolls back, so a listener is
never told about a row it could not read. And it covers every write path by
construction — including the partner webhook, which sets `ready` through a
different repository on the *system* session, and which is exactly the sort of
path application-level notification forgets.
`tests/integration/test_progress_notify.py` pins all three properties.

**Why the payload is only an id.** Subscribers re-read the projection, so a
duplicated, coalesced or out-of-order notification is harmless, there is no
8000-byte payload cap to design around, and no tenant data crosses a channel
every process listens to. The event body is the same
`DocumentDetailResponse` the polling endpoint returns, so the two cannot drift.

**Why it costs so little.** One `LISTEN` connection *per process*, not per
subscriber: 5,000 watchers cost 5,000 sockets and one database connection.
Reads scale with events, not with watchers × seconds — 37–112× less work than
1s polling at the 12-month target:

| | watchers | push | poll @1s |
|---|---|---|---|
| today | 50 | 0.4 events/s | 50 req/s |
| 12-month peak | 5,000 | 134 events/s | 5,000 req/s |

Three details that keep it honest:

- **Coalescing is free.** Each subscriber's queue is `maxsize=1` and drops when
  full — a pending wake already means "re-read", so a second is redundant. The
  debouncer is the queue, with no timer.
- **Every connection opens with a snapshot**, which is what makes reconnection
  need no replay, and what makes the 5-minute connection cap safe rather than
  lossy. A document parked in `awaiting_partner` overnight does not hold a
  socket overnight.
- **A listener reconnect wakes every subscriber**, because notifications that
  arrived while the connection was down are gone.

**Swagger cannot render a stream** — it will appear to hang. Use `curl -N`, or
poll `GET /documents/{id}`, which returns the identical body.

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/documents/$DOC/events
```

Browser `EventSource` cannot send an `Authorization` header, so a real UI would
need a short-lived stream ticket. Not built: there is no UI, and inventing the
ticket endpoint now would be speculative.

## Retry policy: measured, not chosen

The provider mocks sleep *before* the failure check, so a failed attempt costs
the same wall-clock as a successful one. Retrying is expensive here, which is
why the policy is measured. `scripts/simulate_pipeline.py`, 200,000 simulated
pipelines:

| policy | p50 | p95 | p99 | documents given up |
|---|---|---|---|---|
| 1 attempt (no retry) | 18.7s | 26.6s | 28.7s | **80.2%** |
| 3 attempts, no backoff | 25.3s | 42.6s | 51.1s | 14.1% |
| 5 attempts, no backoff | 26.6s | 48.5s | 60.4s | 1.6% |
| **5 attempts, expo 1/2/4/8s** | **28.5s** | **56.9s** | **73.7s** | **1.6%** |
| 5 attempts, expo 5/10/20/40s | 35.3s | 95.1s | 137.3s | 1.7% |

- **Retries are load-bearing.** Without them 80% of documents never reach the
  partner. DBOS ships `retries_allowed=False, max_attempts=3`; taking that
  default would mean a 14% give-up rate.
- **Backoff must stay small.** Exponential-with-5s-base puts p99 past the 120s
  target on its own. These failures are simulated coin flips, not a struggling
  downstream, so patience buys nothing.
- **The headroom is for queueing.** p95 of 56.9s against 120s leaves ~63s,
  which is the budget for waiting for a worker — the real scaling constraint.
- **The fan-out earns ~9s at p95** (56.9s parallel against 65.5s serial).

`tests/unit/test_retry_policy.py` pins these numbers, so lowering them fails the
build rather than silently tripling the give-up rate.

## Data model: two owners, one direction

DBOS checkpoints every step to its own tables in the `dbos` schema. `documents`
and `document_steps` are a **tenant-facing read model** — written by the step
wrappers, read by the API, never orchestrated off. We do not read DBOS's schema;
that is its internal contract, not ours.

The duplication is deliberate and one-way. It is not the two-sources-of-truth
problem a broker creates, where both stores are load-bearing.

Three choices worth naming:

- **`org_id` leads the index** (`ix_documents_org_id_created_at`, from the
  upload work), so a tenant's document list is an index range scan rather than
  a scan-then-filter.
- **All four step rows exist from creation**, written in the same transaction
  as the document. "Not started" is a row with `status=pending`, never a
  missing row, so nothing downstream needs a special case for partial
  progress — and every later writer may assume its row exists.
- **`output` is always `jsonb`.** Real OCR text is megabytes; only a preview is
  projected (`{"chars": n, "preview": "..."}`) and the full text stays in the
  DBOS checkpoint. Inlining it would be ~10GB/day of write amplification at the
  12-month target.

`document_steps` carries a denormalized `org_id`. Every other tenant table keys
its RLS policy on a column it owns, and a policy that had to join back to
`documents` would be both slower and a special case in the migration.

Migration `0003_pipeline` adds `workflow_id`, `partner_job_id` and
`failed_step` to the `documents` table that `0002_documents` created for the
upload path, plus `document_steps` and its policy. `DocumentStatus` grew from
one value to five; it was already a string column rather than a native enum
precisely so that adding values would not need a lock.

## Tenancy

Pipeline workers write these rows with **no user present**, which is the
interesting case. Their organization comes from the workflow's own argument,
which is also what pins `app.current_org_id` on their session — so worker writes
are covered by row-level security exactly like request writes, and a worker
cannot write outside the tenant it was started for.

The work queue is **partitioned by `org_id`**, so one organization uploading
10,000 documents cannot starve another organization's single upload. Without
that, tenant fairness is a queue-ordering accident.

`tests/integration/test_document_tenancy.py` asserts isolation twice: once
through the repository, which filters explicitly, and once through deliberately
unfiltered SQL, which only the database can stop.

## The blocking sleep problem

The provider mocks call a blocking `time.sleep()`, so every in-flight step
holds a thread. The steps are `async` and push the mocks to `asyncio.to_thread`,
which keeps the event loop free but does not remove the thread.

At today's load that is ~0.4 concurrent steps and irrelevant. At the 12-month
target it is ~390, which is too many OS threads to be comfortable. The mock's
sleep stands in for network I/O, so the fix is not more threads — it is that
these become `await`ed HTTP calls once the providers are real, at which point
390 concurrent coroutines is nothing. Flagged rather than solved: building for
it now would be building for a load that does not exist.

`PIPELINE_QUEUE_POLLING_INTERVAL_SECONDS` (default 1.0s) is pure added latency
and lands twice per document, spending ~2s of the ~63s headroom.

## Testing

The provider code is never modified, including by tests. `steps.py` resolves
`random` from its own module globals, so a test can replace **that one name**;
the real `random` module, and everything else using it, is untouched.

- `tests/unit/test_steps_contract.py` diffs the shipped module against the code
  block in `README.md` — the statement itself. Every latency number above
  derives from those mocks, so a well-meaning cleanup fails loudly instead of
  silently invalidating them. Both `ruff format` and its import sorting would
  break the match, which is why the file is excluded from each in
  `pyproject.toml`.
- `tests/unit/` covers the use cases and the projection against fakes; no
  database, no orchestrator.
- `tests/integration/` covers RLS for `documents` and `document_steps`, and the
  real partner sink, against Postgres running the actual migrations.
- The pipeline running end to end is exercised through `docker compose`.

## What I would do with more time

- **Push OCR text into object storage.** The step should return a storage key,
  not megabytes of text. `ObjectStore` already exists for uploaded documents,
  so this is wiring rather than new infrastructure — but it is not wired.
- **A sweep for ghosted partners.** `status = 'awaiting_partner' AND updated_at
  < now() - interval '24h'` has no home yet; today such a document waits
  forever.
- **A stream ticket for browsers**, so `EventSource` can authenticate without
  putting a token in the query string.
- **Dead-letter handling and a replay endpoint.** 1.6% give-up is ~1,600
  documents/day at target. Checkpointing means replay resumes rather than
  restarts; nothing exposes that yet.
- **A load test** that produces the p95 rather than simulating it.
