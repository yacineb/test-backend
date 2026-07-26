# Upload: architecture and rationale

Scope of this document: how a file gets from a client into durable storage with a
row in the database, and why each decision was made. Pipeline execution and
progress reporting are deliberately out of scope here and get their own document.

Every claim below is either arithmetic you can check or a trade-off with its cost
stated. Where something is a known weakness, it is named as one, with the
condition that would force us to fix it.

## 1. The data model

The design is the table. Everything else follows from it.

```sql
CREATE TABLE documents (
    id            uuid          PRIMARY KEY,
    org_id        uuid          NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    uploaded_by   uuid          NOT NULL REFERENCES users(id),
    filename      varchar(255)  NOT NULL,
    content_type  varchar(255)  NOT NULL,
    size_bytes    bigint        NOT NULL,
    sha256        varchar(64)   NOT NULL,
    storage_key   varchar(512)  NOT NULL UNIQUE,
    status        varchar(32)   NOT NULL,
    created_at    timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX ix_documents_org_id_created_at ON documents (org_id, created_at);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE  ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON documents
    FOR ALL
    USING      (org_id = (NULLIF(current_setting('app.current_org_id', true), ''))::uuid)
    WITH CHECK (org_id = (NULLIF(current_setting('app.current_org_id', true), ''))::uuid);
```

`uploaded_by` deliberately has no `ON DELETE` action, unlike `org_id`. Removing
an organization should take its documents with it; removing a *user* should not
destroy records their organization still owns. There is no user-deletion path
yet, and the missing action is what forces that question to be answered
explicitly when one appears rather than silently resolved by a cascade.

Three properties of this table are load-bearing.

### `storage_key` is server-generated: `{org_id}/{document_id}`

No part of it comes from the client. `filename` is stored as *data* — it is
returned in API responses and shown to users, and it is never used to build a
path.

This is not a defence-in-depth measure layered on top of validation; it replaces
validation. There is no `../` to reject, no null byte to strip, no Unicode
normalisation attack to consider, no case-insensitive-filesystem collision,
because no user-controlled string ever reaches a path. A sanitiser is code that
can be forgotten at a new call site. A key format that has nowhere to put
attacker input cannot be forgotten.

The `org_id` prefix is not decoration. On S3 it is the natural boundary for
lifecycle rules, bucket policies, per-tenant metrics, and — if a tenant ever
requires it — a per-tenant KMS key or a separate bucket. Getting that prefix
wrong is a data migration; getting it right costs nothing today.

### Bytes are committed before the row is inserted

The order is: generate a UUID, stream the bytes to storage, and only once the
object is durable, `INSERT` the row.

The alternative — insert `status='uploading'` first, flip it to `uploaded`
afterwards — is more common and worse. It makes "a row exists" and "the object
exists" two different facts, so every reader in the system, forever, has to know
which rows are real. The listing endpoint has to filter. The pipeline has to
guard. Every future feature inherits the check, and the first one that forgets it
produces a 500 on a file that never existed.

With bytes-first there is exactly one invariant, and it is total:

> **Every row in `documents` refers to a complete, durable object.**

The cost is real and it is the right cost to pay: a crash between the storage
commit and the `INSERT` leaves an orphan object with no row. Orphans are cheap
(they are storage, not correctness), they are invisible to every reader, and they
are recoverable by a sweeper that lists keys and diffs them against the table.
That job does not exist yet — it is listed in §7. The failure mode of the
alternative is a corrupt read path; the failure mode of this one is a slightly
larger storage bill until a cron job runs.

### `status` is a column, not a schema decision deferred

Today it only ever holds `uploaded`. It exists now because the pipeline states
land in this column next, and adding it later means a migration on a table that
by then has rows.

## 2. The storage abstraction

```python
class ObjectStore(Protocol):
    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> int: ...
    async def get(self, key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, key: str) -> None: ...
```

Three methods, no configuration knobs, no policy. It is small because the useful
intersection of "a POSIX directory" and "a distributed object store" is small,
and pretending otherwise is how abstractions become lies.

### The contract that makes it honest

The interface would be worthless if it were only a naming convention over two
different sets of semantics. It carries exactly one guarantee, and both backends
implement it natively rather than emulating it:

> **A key either exists complete, or does not exist. There is no partial object,
> under any failure, ever.**

| | POSIX | S3 / object storage |
|---|---|---|
| write | `write()` to `{key}.tmp` | `UploadPart` per chunk |
| commit | `fsync` then atomic `rename` | `CompleteMultipartUpload` |
| abort | `unlink` the temp file | `AbortMultipartUpload` |

`rename(2)` within a filesystem is atomic, and `CompleteMultipartUpload` is
atomic. This is why `put` owns the entire write-commit-abort lifecycle instead of
exposing a file handle for the caller to manage. A handle-based interface
(`open() -> writer`) would push commit ordering onto every call site and would
have no correct S3 implementation at all, because S3 has no handle to hand out.
The interface is shaped by what the backends can actually guarantee, not by what
is convenient to write against a local disk.

The consequence is that a crashed or aborted upload is invisible rather than
merely tidy. Nothing downstream needs to distinguish a complete object from a
truncated one, because a truncated one is not reachable by any key.

### Why the POSIX backend is not a toy

It is the same contract, backed by `write` → `fsync` → `rename`, with blocking
file I/O dispatched through `anyio.to_thread` so it never stalls the event loop.
That last point matters more than it looks: a naive implementation calling
`open()`/`write()` directly in an async handler blocks the entire worker's event
loop for the duration of the disk write, which under concurrent uploads
serialises every other request on that worker — including health checks. It
would appear to work perfectly in development and fall over under load.

### What the interface deliberately does not have

No `exists()`, no `list()`, no `copy()`, no metadata dictionary, no
`presigned_url()`. Not one of them is needed by the current call sites.

`presigned_url()` in particular is the tempting one, since §3 explains that
presigned uploads are the long-term direction. It is still absent, because a
method with one implementation, no caller, and no test is not preparation — it is
an unverified guess about a future interface, and it will be wrong in some
detail that only becomes visible when the second backend is real. The migration
in §3 is a change at one call site behind a stable data model. That is what makes
it cheap, not a placeholder method added a year early.

## 3. Streaming through the API, and exactly when to stop

The bytes currently flow client → API → storage. The API terminates the upload
and streams it onward.

The alternative is presigned URLs: the API hands back a signed URL, the client
`PUT`s straight to S3, and the API never touches the payload. That is the design
that scales, and it is where this goes. The question is only whether it is
warranted now.

### The arithmetic

The stated 12-month target is 100,000 documents/day and 5,000 concurrent users.

- 100,000/day ÷ 86,400 s = **1.16 uploads/s** average.
- Traffic is not uniform. Assuming 80% of it lands in an 8-hour window:
  100,000 × 0.8 ÷ 28,800 s = **2.8 uploads/s** at peak.

At a realistic average PDF size of 2 MB, peak throughput is **5.6 MB/s ≈ 45
Mbit/s**. That is not a meaningful load for a single API process, let alone a
horizontally scaled tier. Connection concurrency is the more interesting number,
and it is also small: at 2.8 uploads/s with a generous 3-second upload duration
over a slow client link, roughly **9 uploads are in flight** at any moment. Each
costs one async task and one spooled temp file.

The presigned design would remove a load that is not present.

### The condition that flips the decision

The variable that matters is not document count, it is *average object size*, and
the relationship is linear. At the 100 MB cap as an average — a document corpus
of scanned, image-heavy PDFs rather than text ones — the same 2.8 uploads/s
becomes **280 MB/s ≈ 2.2 Gbit/s sustained**. At that point the API tier is being
scaled purely to shovel bytes, which is both expensive and pointless, and
presigned uploads become correct.

So the trigger is explicit and measurable rather than a matter of taste:

> **Move to presigned uploads when sustained upload bandwidth through the API
> tier approaches the point where nodes are being added for bandwidth rather than
> for request concurrency.** Instrument `size_bytes` — the data to make this call
> is already being recorded in the table, per upload, from day one.

### Why the migration is cheap when it comes

The presigned flow changes where bytes travel. It does not change the data model,
because the data model was never about transport:

- `storage_key` is already server-generated, so it is already what gets signed.
- Bytes-before-row already matches the presigned lifecycle, where the row is
  written on completion callback after S3 confirms the object.
- The `ObjectStore` contract — complete or absent — is exactly what
  `CompleteMultipartUpload` provides, so the invariant survives unchanged.
- The 100 MB cap moves from application code to an S3 policy
  `content-length-range` condition, which is a stricter enforcement point, not a
  weaker one.

What genuinely gets *added* is the abandoned-upload problem: a signed URL that
is issued and never used, or used and never completed. That is new work, it is
not avoidable, and it is a reason not to take it on before the load justifies it.

### The known cost of the current approach

`UploadFile` means Starlette spools the request body to a local temp file before
the handler runs, so on the POSIX backend each upload is written to local disk
twice: once by the parser, once by the store. That is a real inefficiency and it
is not hidden here.

It is accepted for now because at the sizes computed above it is not the
bottleneck, and because the alternative — parsing multipart incrementally off
`request.stream()` — means hand-rolled multipart handling for a saving that is
currently unmeasurable. The storage interface does not care which side of it the
bytes come from, so this can be replaced without touching the store, the model,
or the API contract. It is named here so it is a decision on record rather than
something discovered later.

## 4. The 100 MB limit is enforced twice, for two different reasons

This is not redundancy. The two checks guard different things and neither one
subsumes the other.

**Layer 1 — ASGI middleware, ~101 MB, whole request body.**
Rejects on `Content-Length` before reading a single byte. When that header is
absent or lying — chunked transfer encoding makes it trivially forgeable — it
wraps `receive` and aborts once the running count crosses the limit.

This layer exists because **`UploadFile` spools before the handler runs**. A cap
implemented only in handler code is not a cap at all: a client streaming 10 GB
would have all 10 GB written to the server's temp directory before a line of
application code executed. The middleware runs ahead of the multipart parser, so
the parser is never handed more than the limit. The ~1 MB of slack above 100 MB
covers multipart boundaries and part headers, which are part of the body but not
part of the file.

**Layer 2 — one pass over the chunks, exactly 100 MB, file bytes only.**
The documented product limit, applied to the file content itself, producing a
clean `413` with a message a client can act on. The same single pass counts bytes
and computes the SHA-256, so integrity costs no extra traversal of the data.

Layer 1 is a resource guard with a fuzzy bound and a blunt failure. Layer 2 is a
precise product rule with a good error message. Collapsing them into one check
means either an imprecise product limit or an unprotected parser.

Layer 1 is hand-written, and that is a deliberate answer to "use a library
instead". There isn't one: no maintained PyPI package implements an ASGI
request-body cap, uvicorn 0.51 exposes no body-size limit (`limit_concurrency`,
`limit_max_requests` and `h11_max_incomplete_event_size` are all about something
else), and Starlette ships only the `413` status constant. In production this
belongs in the reverse proxy — nginx `client_max_body_size`, or the equivalent
ingress annotation — and the middleware stays as the guarantee that holds when
the app is run without one. The security *headers* are a library
(`secure`), because there one exists and is maintained.

The limit is configuration (`MAX_UPLOAD_BYTES`), not a constant — which is what
makes the boundary genuinely testable. Tests set it to a small value and assert
that *exactly* the limit succeeds and limit+1 fails. A hardcoded 100 MB would
mean either pushing 100 MB through the test client on every run, or never testing
the boundary at all and hoping the comparison is not off by one.

## 5. Where tenancy comes from

**Neither `org_id` nor `uploaded_by` is a parameter of the upload API.** There is
no field, header, or query string a client can set to influence either one. Both
are read from the verified access token:

```python
async def upload(ctx: CurrentUser, deps: UploadDepsDep, file: UploadFile) -> ...:
```

`CurrentUser` resolves to an `AuthContext(user_id, org_id)` decoded from the
bearer JWT. A request with no token never reaches the handler; a request with a
token reaches it already scoped. "Upload into another tenant" is not a request
that can be *refused* — it is a request that cannot be *expressed*.

That single source then propagates through four independent layers, and each one
would stop a cross-tenant write on its own:

1. **The use case** takes `ctx` and derives `storage_key = {org_id}/{document_id}`
   from it. The org prefix in object storage is the token's org by construction.
2. **The repository** is built org-scoped (`OrgScopedDocumentRepository(session,
   ctx.org_id)`) and rejects a `Document` whose `org_id` differs — that mismatch
   is a wiring bug, so it raises rather than returning an error to the client.
3. **The session** is an RLS-scoped connection: `app.current_org_id` is set per
   transaction and the application connects as `app_rw`, which does *not* hold
   `BYPASSRLS`.
4. **Postgres** enforces the `org_isolation` policy on `documents` with both
   `USING` and `WITH CHECK`, so a query that loses its `WHERE` clause returns
   nothing and an insert aimed elsewhere is refused by the database.

Layers 1–2 are application code and could be defeated by a bug. Layers 3–4 are
the database, and they hold even if the application code is wrong — which is
exactly why `tests/integration/test_document_isolation.py` asserts through
deliberately *unfiltered* SQL. If those tests pass, Postgres is doing
independent work rather than the `WHERE` clause doing all of it.

The listing endpoint gets isolation from the same place: `list_recent` takes no
org parameter at all, because the repository already carries one.

## 6. What is verified

Claims in this document that are checked by tests rather than asserted:

- **Storage atomicity** — an exception raised mid-stream leaves neither the key
  nor a `.tmp` file behind. This is the §2 contract, tested directly.
- **Cap, layer 1** — an oversized `Content-Length` is rejected without the body
  being read; a chunked body that understates its size is still aborted.
- **Cap, layer 2** — exactly at the limit succeeds, limit+1 fails.
- **Integrity** — bytes read back out of storage hash to the SHA-256 recorded in
  the row.
- **Key hygiene** — a hostile filename (`../../etc/passwd`) appears in the
  `filename` column and nowhere in `storage_key`.
- **Failure atomicity** — when the insert fails, the object it would have
  referenced is removed, so no unreachable blob is left behind.
- **Auth is required** — upload and list both answer `401` without a bearer
  token, and the endpoints carry `security` in the OpenAPI schema.
- **Tenancy comes only from the token** — `org_id` and `uploaded_by` on the
  stored document match the token's claims; smuggling `org_id` in as an extra
  form field changes nothing.
- **RLS covers `documents`** (`tests/integration/`, real Postgres) — an
  unfiltered `SELECT` on a session scoped to one org cannot see another's rows,
  an `INSERT` aimed at another org is refused by the `WITH CHECK` policy, and a
  connection that never set `app.current_org_id` sees zero rows rather than all
  of them.

### Measured against the running stack

The two-layer limit from §4 is the one claim worth showing at real size rather
than at the 1KB the unit tests use. Against `docker compose up`, with the
production 100 MiB limit:

| request | result | time |
|---|---|---|
| exactly 104,857,600 bytes | `201` | 0.25 s |
| 104,857,601 bytes (limit + 1) | `413`, use-case layer | 0.13 s |
| 110,000,000 bytes | `413`, middleware layer | **0.0015 s** |

The last row is the point of the split. The middleware answers in under two
milliseconds because it refuses on `Content-Length` and never reads the body;
the middle row takes ~85× longer precisely because the body *was* buffered
before the exact per-file check could run. A single-layer design gets one of
those two behaviours, never both.

After all three, the storage volume held only the objects from the successful
uploads: no partial files, no `.tmp` debris from either rejection.

The same run confirmed the §5 claim end to end. Alice's access token carries
`org: 1ae48790-…`, and the object written for her upload landed at
`1ae48790-…/9a5a4311-…` — the storage prefix is the token's org claim, not
anything the request supplied.

## 7. Deliberately not built

Listed because "not yet built" and "not thought about" are different things, and
the difference should be visible.

- **Orphan sweeper.** The counterpart to bytes-before-row (§1). Not needed until
  crash-during-upload is observed with any frequency; the data to detect orphans
  (keys vs. rows) is available whenever it is wanted.
- **Idempotency.** A retried upload creates a second document. The answer is an
  `Idempotency-Key` header with a uniqueness constraint, not client-side
  deduplication. Not built because no client currently retries automatically.
- **Formats other than PDF.** Only `application/pdf` is accepted; see §9. The
  accepted set is a constant rather than an env var, because which formats the
  pipeline can process is a product decision, not a per-deployment one.
- **Deduplication.** `sha256` is recorded, which is the hard part. Whether
  identical bytes uploaded by two orgs share one object is a tenancy and deletion
  question, not a storage one, and it is not answered here.
- **Download endpoint.** `ObjectStore.get` exists and is tested; no route exposes
  it yet.
- **Virus scanning, encryption at rest, per-tenant keys.** All are storage-layer
  concerns that the `org_id` key prefix leaves room for.

## 8. Exercising it

`docker compose up --build`, then Swagger at http://localhost:8000/docs.

Migrations and demo seeding run on startup, so two tenants exist immediately.
Sign in as one of the seeded users, then use the access token. In Swagger,
`POST /auth/login` gives you a token and the **Authorize** button takes it.

| Organization | Email | Password |
|---|---|---|
| Acme Corp | `alice@acme.example.com` | `password123` |
| Globex | `bob@globex.example.com` | `password123` |

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@acme.example.com","password":"password123"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -X POST http://localhost:8000/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@some.pdf;type=application/pdf"

curl http://localhost:8000/documents -H "Authorization: Bearer $TOKEN"
```

Nothing in either request names an organization. Log in as `bob@globex.example.com`,
upload there, and list again as Alice: neither sees the other's documents, and
the only thing that changed was the token.

## 9. Only PDFs, decided by the bytes

The corpus is PDFs, so `application/pdf` is the only accepted type. The check
runs on the file's leading bytes, never on the `Content-Type` the client sent.

That distinction is the whole point. A request header is a claim made by the
party we are trying to validate; treating it as evidence means the check can be
defeated by editing one string. Renaming `payload.png` to `report.pdf` and
declaring `Content-Type: application/pdf` gets a `415`, because the first eight
bytes still say PNG.

The use case therefore takes **no `content_type` argument at all**. The router
does not forward `file.content_type`, so there is no path by which a client's
claim can reach the decision or the stored row — the sniffed type is what gets
recorded. A genuine PDF uploaded as `application/octet-stream` is accepted and
stored as `application/pdf`.

### Rejecting before anything is written

The first 4KB are pulled off the stream, checked, and then replayed ahead of the
rest, so the decision happens before `ObjectStore.put` is called. A rejected
upload never opens a temp file, rather than opening one and cleaning it up.

That peek also subsumes the empty-file case: a stream with no head is empty, and
is known to be empty before any write. The post-write `size == 0` check that
used to follow the upload is gone, because it became unreachable.

### puremagic, not python-magic

`python-magic` binds to the system `libmagic` shared object. The runtime image is
distroless — no shell, no package manager — so that library would have to be
vendored in by hand and kept in step with the base image. `puremagic` is pure
Python with no dependencies and needs nothing from the image.

The detector sits behind a `ContentTypeDetector` port with the adapter in
`app/infrastructure/`, matching how every other third-party dependency is
handled here (`argon2` behind `PasswordHasher`, `PyJWT` behind `TokenService`),
so the application layer keeps importing nothing but the standard library.

### What this check is and is not

It verifies the file *starts* like a PDF. It is not a parser and does not prove
the document is well-formed, non-malicious, or renderable — a PDF header glued
to arbitrary bytes passes. Real validation belongs to the OCR stage, which has
to parse the file anyway; this check exists to reject obvious mismatches cheaply
at the boundary rather than to guarantee integrity.
