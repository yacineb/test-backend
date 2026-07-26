"""What the external partner tells us about a job it took over.

`external_call` hands the partner a document and gets back an opaque job_id;
the outcome arrives later as a signed webhook. This module is that outcome,
and nothing else — no HTTP, no signature, no persistence.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class PartnerJobStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PartnerNotification:
    """One verified partner notification.

    `job_id` is the only join key we have: the partner knows nothing about our
    organizations or document ids, so the notification names no tenant and
    cannot be aimed at one. Resolving it to a document is the sink's job.
    """

    job_id: str
    status: PartnerJobStatus
    result: dict[str, Any] | None
    occurred_at: datetime  # timezone-aware; the edge rejects naive timestamps

    def is_stale(self, now: datetime, tolerance: timedelta) -> bool:
        """True when occurred_at sits outside the accepted window.

        Both directions on purpose: a partner clock running ahead of ours is
        as suspicious as a replayed capture from last week.
        """
        return abs(now - self.occurred_at) > tolerance
