# Incoming partner webhook

The partner that `external_call` hands a document to answers asynchronously:
`external_call` returns an opaque `job_id`, and the real outcome arrives later
as a `POST /webhooks/partner`. Until that notification is received *and
verified*, the document is not `ready`.

## Data first

One value crosses the boundary, and everything else is machinery around it:

```python
@dataclass(frozen=True, slots=True)
class PartnerNotification:
    job_id: str  # opaque, minted by the partner
    status: PartnerJobStatus  # completed | failed
    result: dict[str, Any] | None
    occurred_at: datetime  # partner-side event time, inside the signature
```

`job_id` is the only join key. The partner knows nothing about our
organizations, users or document ids, which is the point: the webhook carries
no tenant identity, so tenancy is resolved *by us*, by looking up which
document is waiting on that `job_id`. A webhook can never be made to touch an
organization it was not already pointed at, because the caller never names one.

## Authenticity: HMAC over the raw bytes

`X-Partner-Signature` is `HMAC-SHA256(raw_body, PARTNER_HMAC_SECRET)`, hex.

Two properties the implementation depends on:

- **The raw bytes are signed, not the parsed model.** `json.dumps` of a parsed
  body differs from what the partner sent by whitespace and key order, so
  re-serializing before verifying would fail on valid requests and, worse,
  invite someone to "fix" it by loosening the check. `HmacSha256Signer` takes
  `bytes` and nothing else.
- **Verification happens before parsing.** It is a FastAPI dependency, not the
  first four lines of the handler: FastAPI solves dependencies before it
  validates the request body, so an unsigned request is `401` and the parser
  never sees the payload. Verified by test, not by assertion —
  `test_unsigned_and_malformed_is_401_not_422`.

Comparison is `hmac.compare_digest`, so a wrong signature costs the same time
as a right one.

## Freshness: HMAC does not stop replay

A captured valid request stays valid forever — the signature says *authentic*,
not *recent*. `occurred_at` is inside the signed payload, so it cannot be
tampered with, and notifications outside `PARTNER_WEBHOOK_TOLERANCE_SECONDS`
(default 300) are rejected with `400`. The window is checked in both
directions: a partner clock running ahead of ours is as suspicious as a replay.
Set the tolerance to `0` to disable the check.

This is a window, not idempotency. Inside the window a retry is still a
duplicate, which is the sink's problem — see below.

## Status codes

| Situation | Code | Why |
|---|---|---|
| Verified, accepted | `202` | The outcome may still be applied asynchronously |
| Missing or wrong signature | `401` | Body is never parsed |
| Signature valid, body malformed | `422` | Authentic sender, broken payload |
| `occurred_at` outside the window | `400` | Retrying the same bytes will not help |
| Unknown `job_id` | `404` | We never issued it; do not retry |

`401` here deliberately carries no `WWW-Authenticate: Bearer` — the route is
not part of the JWT surface and the partner has no bearer token to offer.

## What is stubbed, and what replaces it

`external_call` and the documents table do not exist on this branch, so there
is nothing to resolve a `job_id` against. The seam is one port:

```python
class PartnerJobSink(Protocol):
    async def deliver(self, notification: PartnerNotification) -> None: ...
```

The stub, `InMemoryPartnerJobSink`, accepts every `job_id` and keeps the last
100 notifications in a `deque` so the endpoint is exercisable from `/docs` and
from tests today. It is process-local: with more than one uvicorn worker, only
the worker that handled the request remembers it.

When the pipeline branch lands, the real adapter looks up the document by
`partner_job_id`, applies the outcome, and takes on two obligations the stub
does not have:

- **Raise `UnknownPartnerJob`** when no document is waiting on that `job_id`
  (already wired to `404`).
- **Be idempotent.** Partners retry; the same `job_id` will arrive twice, and a
  duplicate must not double-apply. The natural implementation is a conditional
  update on the document's current state, not a separate dedup table.

One line in `app/api/deps.py` changes. Nothing else moves.

## Testing it from Swagger

The signature must cover the exact bytes on the wire, which makes the endpoint
untestable by hand without a helper. `POST /webhooks/partner/sign` signs
exactly the bytes you post to it:

1. Type the notification into `/webhooks/partner/sign` and execute.
2. Copy the returned `signature`.
3. Send the **same text** to `/webhooks/partner` with that signature in the
   `X-Partner-Signature` header.

The helper takes the raw body rather than a parsed model on purpose. A parsed
model would have to be re-serialized before signing, and the only bytes that
would then verify are the ones inside the JSON-escaped `body` field of the
helper's response — which a human has to unescape by hand before pasting. Its
`body` field is an echo, for recovering what was signed, not a step in the
workflow. Editing the text between the two calls, even by a space, invalidates
the signature; that is the check doing its job, not a bug.

The handler declares both a parsed `PartnerWebhookRequest` and the raw
`Request`. The parsed one is never read; it is there so FastAPI validates the
payload and documents an editable textarea in `/docs` exactly as it does for
every other route, while the signature still covers `request.body()`. Signing a
body the webhook would reject only moves the `422` one request later.

The helper is a forgery oracle: anyone who can call it can sign anything. It is
on by default so `docker compose up` gives a working `/docs`, and it is turned
off with `PARTNER_WEBHOOK_SIGNING_HELPER=false` anywhere the secret is real.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PARTNER_HMAC_SECRET` | dev placeholder | Shared secret, out-of-band. **Override in production.** |
| `PARTNER_WEBHOOK_TOLERANCE_SECONDS` | `300` | Replay window; `0` disables |
| `PARTNER_WEBHOOK_SIGNING_HELPER` | `true` | Exposes `/webhooks/partner/sign` |
