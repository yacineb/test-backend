"""Stub PartnerJobSink, standing in until the pipeline branch lands.

`external_call` and the documents table do not exist yet, so there is nothing
to resolve a job_id against. This adapter accepts every job_id and remembers
the last few notifications, which is enough to exercise POST /webhooks/partner
end to end from /docs and from tests today.

Process-local: with more than one uvicorn worker, only the worker that handled
the request remembers it. That is fine for a stub and fatal for the real thing.

Replacing it means writing an adapter that looks the document up by
partner_job_id, raises UnknownPartnerJob when there is none, and applies the
outcome idempotently. One line in app/api/deps.py points at it; nothing else
moves. See docs/incoming-webhook.md.
"""

from collections import deque

from app.domain.partner import PartnerNotification


class InMemoryPartnerJobSink:
    def __init__(self, maxlen: int = 100) -> None:
        self.received: deque[PartnerNotification] = deque(maxlen=maxlen)

    async def deliver(self, notification: PartnerNotification) -> None:
        self.received.append(notification)
