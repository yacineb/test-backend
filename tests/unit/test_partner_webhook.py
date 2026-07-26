from datetime import timedelta

import pytest

from app.application.deps import WebhookDeps
from app.application.webhooks import receive_partner_notification
from app.domain.errors import StaleWebhook, UnknownPartnerJob
from app.domain.partner import PartnerJobStatus, PartnerNotification
from app.infrastructure.partner_jobs import InMemoryPartnerJobSink
from tests.fakes import FakeClock, RejectingPartnerJobSink

pytestmark = pytest.mark.anyio

TOLERANCE = timedelta(minutes=5)


def make_notification(clock: FakeClock, offset: timedelta = timedelta()):
    return PartnerNotification(
        job_id="j_abc123def4567890",
        status=PartnerJobStatus.COMPLETED,
        result={"indexed_at": "2026-05-21T14:23:11Z"},
        occurred_at=clock.now() + offset,
    )


def make_webhook_deps(clock: FakeClock, tolerance: timedelta = TOLERANCE):
    return WebhookDeps(sink=InMemoryPartnerJobSink(), clock=clock, tolerance=tolerance)


async def test_a_fresh_notification_reaches_the_sink():
    clock = FakeClock()
    deps = make_webhook_deps(clock)
    notification = make_notification(clock)

    await receive_partner_notification(deps, notification)

    assert list(deps.sink.received) == [notification]


async def test_a_replayed_notification_is_rejected():
    clock = FakeClock()
    deps = make_webhook_deps(clock)
    notification = make_notification(clock)

    clock.advance(TOLERANCE + timedelta(seconds=1))

    with pytest.raises(StaleWebhook):
        await receive_partner_notification(deps, notification)
    assert not deps.sink.received


async def test_a_notification_from_the_future_is_rejected():
    """A partner clock running ahead is as suspicious as a replay."""
    clock = FakeClock()
    deps = make_webhook_deps(clock)

    with pytest.raises(StaleWebhook):
        await receive_partner_notification(
            deps, make_notification(clock, offset=TOLERANCE + timedelta(seconds=1))
        )


async def test_the_edge_of_the_window_is_still_accepted():
    clock = FakeClock()
    deps = make_webhook_deps(clock)
    notification = make_notification(clock, offset=-TOLERANCE)

    await receive_partner_notification(deps, notification)

    assert len(deps.sink.received) == 1


async def test_zero_tolerance_disables_the_freshness_check():
    clock = FakeClock()
    deps = make_webhook_deps(clock, tolerance=timedelta())
    notification = make_notification(clock, offset=-timedelta(days=30))

    await receive_partner_notification(deps, notification)

    assert len(deps.sink.received) == 1


async def test_an_unknown_job_id_propagates_from_the_sink():
    """The contract the pipeline branch has to honour when it replaces the stub."""
    clock = FakeClock()
    deps = WebhookDeps(sink=RejectingPartnerJobSink(), clock=clock, tolerance=TOLERANCE)

    with pytest.raises(UnknownPartnerJob):
        await receive_partner_notification(deps, make_notification(clock))
