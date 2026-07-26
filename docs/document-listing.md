# Listing documents: architecture and rationale

Follow-up to [docs/upload-architecture.md](upload-architecture.md), which covers
how a document gets *in*. This one covers how an organization reads its
documents back: what a listing row is, why paging is by cursor rather than
offset, and what it costs.

Every number below was measured against Postgres 17 with 2,000,031 documents —
500,000 in the caller's organization — and the query plans are reproduced. Where
something is a known weakness it is named as one, with the condition that would
force us to fix it. §8 argues that the whole endpoint is the wrong long-term
shape, and why it was still built this way today.

## 1. What a listing row is

The requirement is a document index showing, per document: its name, its id, who
imported it, where it is in processing, and when it arrived. That is the shape:

```python
@dataclass(frozen=True, slots=True)
class DocumentSummary:
    id: UUID
    filename: str
    status: DocumentStatus
    created_at: datetime
    uploader_id: UUID
    uploader_name: str
    uploader_email: str
```

It is deliberately **not** a `Document`. A `Document` is the upload record —
`sha256`, `storage_key`, `content_type`, `size_bytes` — and none of that is on
this screen. Two shapes means the list query reads seven columns instead of
eleven, and it means adding a field to the upload record does not silently widen
every listing response.

`uploader_name` and `uploader_email` are not on `documents`; they come from a
join to `users` (§5). Returning only `uploaded_by` as a raw UUID — which is what
the endpoint did before — pushes every client into an N+1 lookup to render a
name that the database could have supplied in the same index scan.

### `status` is the pipeline's column

It holds `uploaded`, `processing`, `awaiting_partner`, `ready` or `failed` —
the pipeline's own summary of itself, written by the projection in
[docs/pipeline.md](pipeline.md). The listing reads that column and nothing else
of the pipeline: **this endpoint did not change when the pipeline landed, and
will not change when a state is added**, because it reads the column rather than
enumerating it. That is also why it is serialised as a string rather than an
OpenAPI enum — a new state must not turn old clients into validation errors.

What the listing deliberately does *not* carry is the four per-step rows
(`ocr`, `metadata`, `chunking`, `external_call`) with their attempt counts and
errors. Those are a detail view's concern and `GET /documents/{id}` serves them;
loading them per listed row is exactly the N+1 this query exists to avoid.
`failed_step` is the borderline case — it is one column, and a list that shows
`failed` without saying where is mildly unhelpful — but it is left out because
nothing has asked for it, and adding a field later is cheap while removing one
is not.

## 2. Why the page is a position, not a count

`OFFSET n` answers "skip n rows" by *producing* n rows and throwing them away.
The cost is linear in how far you have scrolled, and it is paid on every request.

The seek does not skip. It asks for the rows *after a position*, and the
position is the sort key:

```sql
SELECT … FROM documents d JOIN users u ON u.id = d.uploaded_by
WHERE  d.org_id = :org AND u.org_id = :org
  AND  (d.created_at, d.id) < (:cursor_created_at, :cursor_id)
ORDER BY d.created_at DESC, d.id DESC
LIMIT :limit + 1;
```

Measured, `LIMIT 50`, 500,000 documents in the org:

| position in the listing | `OFFSET` | keyset |
|---|---|---|
| page 1 | 4.0 ms | 0.49 ms |
| 1,000 deep | 0.65 ms | 1.3 ms |
| 10,000 deep | 129 ms | 0.74 ms |
| 100,000 deep | 168 ms | 0.33 ms |
| 400,000 deep | 197 ms | 0.36 ms |

Two things in that table are worth stating plainly rather than glossing over.

**At page 20, offset is faster.** 0.65 ms against 1.3 ms. Anyone claiming keyset
pagination is universally quicker is selling something; at shallow depth both
plans are an index scan and the offset one has a simpler predicate.

**The degradation is a cliff, not a slope.** Between 1,000 and 10,000 rows deep
the planner stops believing the index is worth it and switches to a parallel
sequential scan plus an external merge sort — 19 MB of temp files written to
disk, per request, to return fifty rows:

```
Limit  (actual rows=50)
  ->  Gather Merge  (actual rows=400050)
        ->  Sort  (actual rows=133574 loops=3)
              Sort Method: external merge  Disk: 19000kB
              ->  Parallel Seq Scan on documents d  (actual rows=166667 loops=3)
                    Rows Removed by Filter: 500010
Execution Time: 272.323 ms
```

Against the keyset plan at the same position:

```
Limit  (actual rows=51)
  ->  Index Scan Backward using ix_documents_org_id_created_at_id on documents d
        Index Cond: ((org_id = …) AND (ROW(created_at, id) < ROW(…)))
Execution Time: 0.137 ms
```

The keyset row count is 51 whether you are on page 1 or page 8,000, which is the
actual property being bought: **the cost of a page does not depend on how many
pages precede it.**

### The correctness reason, which matters more than the speed

Offset pages are defined against a list that is moving. Insert one document at
the head while a client is on page 1 and every subsequent `OFFSET` is shifted by
one: the row that was last on page 1 reappears first on page 2, and one row is
skipped entirely. On a listing sorted newest-first, in a system whose whole
purpose is ingesting documents, inserts at the head are not an edge case — they
are the steady state.

A position-based page does not have this failure. The cursor names a row, and
the next page starts strictly after that row, no matter how many documents
arrived in the meantime. `test_paging_is_stable_when_a_document_is_added_mid_scroll`
in `tests/integration/` is exactly this scenario against real Postgres.

## 3. Why the cursor is `(created_at, id)` and not `created_at`

`created_at` alone is not a position, because it is not unique. Two documents
can share a timestamp — trivially so for a bulk import, where thousands land in
the same transaction. A cursor that cannot separate rows within a tie either
re-shows them (`<=`) or skips them (`<`). There is no third option, and both are
silent.

`id` is the primary key, so `(created_at, id)` is unique by construction, and
the strict row-value comparison `(created_at, id) < (:ts, :id)` is total. This is
the whole reason the sort has a second key: not aesthetics, correctness.

`tests/integration/test_document_listing.py::test_paging_over_a_shared_timestamp_loses_nothing`
inserts nine documents at one identical timestamp and pages through them two at a
time, forcing a page boundary inside the tie.

### Row-value comparison, not a hand-written disjunction

The equivalent condition can be written as:

```sql
created_at < :ts OR (created_at = :ts AND id < :id)
```

It is not written that way. `(a, b) < (x, y)` is one expression that Postgres
turns into an index condition; the disjunction is two branches, is easy to get
subtly wrong (`<=` in the wrong half loses or repeats a row), and does not
resolve to a single index scan.

## 4. The index earns its migration

Migration `0004` replaces `(org_id, created_at)` with `(org_id, created_at, id)`.
That is not tidiness. Without `id`, the index can only answer the `created_at`
half of the seek; the row-value comparison drops to a filter over the heap, and
inside a tie that filter has to walk the entire tie.

Measured with a cursor 25,000 rows into a 50,000-row block sharing one timestamp:

| index | seek predicate resolves as | rows scanned | buffers | time |
|---|---|---|---|---|
| `(org_id, created_at, id)` | `Index Cond: … ROW(created_at, id) < ROW(…)` | 51 | 55 | **0.177 ms** |
| `(org_id, created_at)` | `Index Cond` on `created_at` only, `ROW(…)` as `Filter` | 24,999 | 2,024 | **18.9 ms** |

107× on time, 37× on buffers, for one extra index column on a table that is
already indexed on that prefix.

The columns are ascending even though the listing is descending: for a fixed
`org_id`, Postgres scans a btree backwards. An explicit `DESC` index would be
needed only for a *mixed* ordering, which this is not.

The migration creates the new index before dropping the old one, so the table is
never without an index serving the list query.

## 5. The uploader join

```sql
JOIN users u ON u.id = d.uploaded_by
WHERE d.org_id = :org AND u.org_id = :org
```

An **inner** join, and that is a decision. It is safe because `uploaded_by` is
`NOT NULL` with a foreign key to `users`, and it is only ever written from the
same access token that supplies `org_id` — so the uploader is always an existing,
visible row of the caller's own organization. There is no user-deletion path, and
the foreign key deliberately has no `ON DELETE` action (see the upload doc §1),
so a user with documents cannot be removed out from under this.

The alternative, a left join, would make `uploader_name` and `uploader_email`
nullable through every layer — domain, schema, and every client — to model a
state that cannot occur. That is special-case handling for a case the data model
already rules out.

The `u.org_id = :org` predicate is redundant three times over: RLS covers
`users`, the org check on `documents` already constrains the join, and the
invariant above holds. It is there for the same reason the repositories filter on
`org_id` at all — a misconfigured database should not be a silent cross-tenant
read. It costs nothing; `users` is small and the planner materialises it once.

**Known cost:** the join reads `users` on every page. At the current scale that
is a single-row sequential scan the planner caches (`Buffers: shared hit=1`), and
it does not appear in the timings above. If organizations grow to thousands of
users this becomes an index lookup on `users.id`, which is the primary key, so
there is nothing to do about it in advance.

## 6. The cursor is opaque and unsigned

The wire form is base64url of `{created_at ISO 8601}|{uuid}`, unpadded:

```
MjAyNi0wNy0yNlQxMTowMDo1My4yNDU1OTcrMDA6MDB8NWM3OWQ3OTktODU4Ni00NWVmLThlMWItNWY4N2YxOWQ5NTVi
```

**Encoded, because a readable cursor becomes a public API.** A client that can
see `2026-07-26T11:00:53.245597+00:00|5c79d799-…` will start constructing them,
and from that moment the sort key can never change without breaking those
clients. Opaque, the sort key stays an implementation detail — which matters
because it *will* change: sorting by `status` or by uploader means a different
key, and that must not be a breaking change.

**Unsigned, because it carries no authority.** An HMAC here would protect
nothing: the organization comes from the verified bearer token and is enforced
again by row-level security, so a forged cursor can at most start the caller's
own listing at a position of their choosing — which is precisely what the
parameter is for. Verified end to end: Bob replaying a cursor Alice issued gets
`{"items": [], "next_cursor": null}`, never one of Alice's rows. Signing it would
add a key to rotate and a second failure mode in exchange for nothing.

**Precision is load-bearing.** `created_at` is `timestamptz` — microseconds — and
`datetime.isoformat()` round-trips microseconds exactly. A codec that truncated
to seconds would not fail loudly; it would silently skip or repeat rows at every
page boundary where two documents fall in the same second. That is what
`tests/unit/test_document_cursor.py` pins down.

A naive (timezone-less) timestamp is rejected at decode. Left through, it would
be compared against `timestamptz` and surface as a driver error — a 500 for
malformed client input. It is a 400 instead.

## 7. What is verified

Claims above that are checked by tests rather than asserted:

- **Round-trip is exact to the microsecond**, and one microsecond of difference
  produces a different cursor.
- **Garbage cursors are 400, not 500** — non-base64, valid base64 that is not a
  cursor, unparseable timestamp, non-UUID id, missing separator, non-UTF-8
  payload, and naive timestamps.
- **The cursor is opaque** — neither the id nor the year appears in the token,
  and it needs no percent-encoding.
- **Paging visits every document exactly once**, walked end to end through the
  encoded cursor a client would actually use, both against the fake and against
  real Postgres.
- **Ties do not lose rows** (`tests/integration/`) — nine documents at one
  identical timestamp, paged two at a time.
- **Inserts mid-scroll do not shift the page** (`tests/integration/`) — the
  offset failure mode, asserted not to occur.
- **A full last page reports no next cursor** — `next_cursor` is derived from a
  row that was read, so following it never lands on an empty page, and a client
  loop terminates without a trailing empty request.
- **A cursor past the end yields an empty page**, not an error and not a
  wrapped-around first page.
- **The uploader comes from the join** — two users in one org, each document
  reporting its own uploader's name and email.
- **Listing requires a bearer token** and is scoped to that token's org through
  RLS (`tests/integration/test_document_isolation.py`).

### Exercised against the running stack

Five uploads as `alice@acme.example.com`, paged two at a time:

```
GET /documents?limit=2
{"items":[{"filename":"f5.pdf", "uploaded_by":{"full_name":"Alice Martin", …}}, {"filename":"f4.pdf", …}],
 "next_cursor":"MjAyNi0wNy0yNlQxMTowMDo1My4yNDU1OTcrMDA6MDB8NWM3OWQ3OTktODU4Ni00NWVmLThlMWItNWY4N2YxOWQ5NTVi"}

GET /documents?limit=2&cursor=MjAyNi0w…    → f3.pdf, f2.pdf, next_cursor set
GET /documents?limit=50                    → f5…f1, next_cursor null
GET /documents?cursor=zzzz                 → 400 {"detail":"invalid cursor"}
```

## 8. Longer term this should be GraphQL, and that is not a small remark

This endpoint is the point where REST starts costing more than it returns, and
the reason is visible in the design above rather than theoretical.

**The split already happened, and it was forced.** There are now two shapes over
one table: `DocumentSummary` here, and the per-step detail behind
`GET /documents/{id}`. Nobody chose two — the alternative was one response
carrying four step rows per listed document, which is an over-fetch on every
list view. That is REST's only lever: when the field set diverges, add an
endpoint. A third consumer with a third field set means a third endpoint, and
each one is a hand-written query, a hand-written schema, and a hand-written test.

**The uploader join shows the same seam one level down.** It is unconditional, so
a client that only wants filenames still pays for it. Embedding was the right
call — the alternative was every caller doing N+1 lookups — but it is a decision
made *for* all clients because REST has nowhere to put "it depends". Under
GraphQL, `uploadedBy { fullName }` is a resolver the client opts into, and the
join simply does not run for the client that did not ask.

**And the pressure is increasing, not theoretical.** Documents already carry a
workflow id, a partner job id, a failed step and four step rows; tags and
extraction results are the obvious next ones. Each addition re-runs the same
argument: embed and over-fetch, or add a round trip.

**Paging is per-endpoint plumbing.** Cursor, `next_cursor`, the `limit + 1`
trick, and the codec in `app/api/cursors.py` are all hand-rolled here and would
be hand-rolled again, identically, for every future list. GraphQL has a
specified answer — the Relay connection spec — with `edges`, `cursor`,
`pageInfo`, and `hasNextPage`, which is the same design as §2 with the bikeshed
already painted and client tooling that understands it without being told.

**Filtering is where this endpoint will next be found wanting.** "Documents
still processing", "documents Alice imported last week", "failed since
Tuesday" — each is a query parameter, each interacts with the sort key and
therefore the cursor, and the combinations grow multiplicatively. Bolting them
onto this handler produces exactly the pile of optional parameters and
conditional `WHERE` clauses that turns a clean query into a rats nest. A schema
with a typed filter input is the difference between adding a field and adding a
branch.

### Why it is REST today anyway

Because two endpoints is not yet a problem, and the cost of being wrong is low.

There is one consumer, one list, and one sort order. Introducing a GraphQL layer
now would mean a schema, an execution layer, resolver-level authorization, query
depth and complexity limits, and a persisted-query story — before a second
consumer exists to justify any of it. GraphQL's per-field authorization is
genuinely harder than one dependency on one route, and getting it wrong in a
multi-tenant system is a data leak, not a performance regression.

What makes the eventual migration cheap is that the parts worth keeping are
already independent of the transport:

- `DocumentSummary` and `DocumentPage` are domain types with no FastAPI or
  Pydantic in them; a GraphQL resolver returns the same objects.
- `DocumentRepository.list_page(limit, after)` is already a Relay-shaped
  connection query. `after` *is* Relay's `after`.
- The keyset key, the index, and the `limit + 1` next-page probe are all in the
  repository, not the router.

What would be thrown away is `app/api/routers/documents.py` and the response
schemas — roughly sixty lines. **That is the actual argument: the endpoint is
disposable and the data model is not**, so building the simple thing now does not
buy a rewrite later.

The trigger is not a date. It is the second or third consumer with a different
field set, or the first filter combination that makes this handler grow a
conditional query builder. Either one, and the balance flips.

## 9. The offset parameter is gone

`GET /documents?limit=&offset=` is replaced, not supplemented. `offset` is no
longer accepted and is silently ignored if sent — a request carrying it returns
the first page, which is what a cursor-less request means.

This is a break, and it is deliberate. The endpoint shipped with the upload work
a handful of commits ago and has no external consumers, so "do not break
userspace" has nothing to protect here. The alternative — accepting both, with a
rule for which wins — means two orderings to keep consistent, two code paths in
the repository, and a permanent invitation to use the one that falls off a cliff
at page 200. One pagination scheme per endpoint.

If a consumer *had* existed, the answer would have been a new endpoint rather
than a coexistence flag, and the old one deprecated with a date.

## 10. Deliberately not built

Listed because "not yet built" and "not thought about" are different things.

- **Filtering by status, uploader, or date range.** The obvious next request now
  that `status` has five real values — "show me everything still processing",
  "show me what failed" — and the one §8 argues belongs in a different API shape.
  Each filter also changes which index serves the query: a `status` predicate on
  `(org_id, created_at, id)` is a filter, not a seek, so a selective one would
  want its own index. Adding them without measuring would be guessing.
- **Sorting by anything else.** Filename or status ordering means a different
  cursor key and a different index. The cursor being opaque is what makes that
  additive rather than breaking.
- **Total count.** `count(*)` over the org is the exact scan the keyset design
  exists to avoid, so a UI wanting "showing 50 of 12,431" gets an unbounded query
  back. If it is genuinely needed, the answer is an approximate count from
  `pg_class.reltuples` or a maintained counter, not `SELECT count(*)` per page.
- **Paging backwards.** The mirror of the same predicate (`>` with the ordering
  flipped, then reversed in the application), plus a `prev_cursor`. Not built
  because nothing asks for it: the client walks forward and keeps what it has
  seen.
- **A download endpoint.** `ObjectStore.get` exists and is tested; no route
  exposes it. Listing documents you cannot fetch is the current state, and it is
  the upload doc's §7 item, not this one's.

## 11. Exercising it

Same setup as the upload doc — `docker compose up --build`, Swagger at
http://localhost:8000/docs, seeded users in
[CONTRIBUTING.md](../CONTRIBUTING.md).

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@acme.example.com","password":"password123"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# First page.
curl -s "http://localhost:8000/documents?limit=2" -H "Authorization: Bearer $TOKEN"

# Walk to the end: feed next_cursor back until it comes back null.
curl -s "http://localhost:8000/documents?limit=2&cursor=<next_cursor>" \
  -H "Authorization: Bearer $TOKEN"
```

Nothing in either request names an organization. Log in as
`bob@globex.example.com` and replay Alice's cursor: the response is an empty
page, not Alice's documents.
