"""Fan-out and coalescing, without a database or a connection."""

import asyncio
from uuid import uuid4

import pytest

from app.infrastructure.progress import ProgressHub, asyncpg_dsn

pytestmark = pytest.mark.anyio


async def test_a_subscriber_is_woken_by_its_own_document():
    hub = ProgressHub()
    document_id = uuid4()

    async with hub.subscribe(document_id) as changed:
        hub.publish(document_id)

        await asyncio.wait_for(changed.get(), timeout=1)


async def test_a_subscriber_is_not_woken_by_another_document():
    hub = ProgressHub()

    async with hub.subscribe(uuid4()) as changed:
        hub.publish(uuid4())

        assert changed.empty()


async def test_every_subscriber_to_one_document_is_woken():
    hub = ProgressHub()
    document_id = uuid4()

    async with (
        hub.subscribe(document_id) as first,
        hub.subscribe(document_id) as second,
    ):
        hub.publish(document_id)

        assert not first.empty()
        assert not second.empty()


async def test_repeated_notifications_coalesce_into_one_wake():
    """The queue is the debouncer. Three notifications before the reader gets
    round to it mean the same thing as one: re-read."""
    hub = ProgressHub()
    document_id = uuid4()

    async with hub.subscribe(document_id) as changed:
        for _ in range(3):
            hub.publish(document_id)

        assert changed.qsize() == 1


async def test_wake_everyone_reaches_all_documents():
    """Used after a listener reconnect, when notifications were missed."""
    hub = ProgressHub()
    first_id, second_id = uuid4(), uuid4()

    async with (
        hub.subscribe(first_id) as first,
        hub.subscribe(second_id) as second,
    ):
        hub.wake_everyone()

        assert not first.empty()
        assert not second.empty()


async def test_subscribers_are_forgotten_on_exit():
    """A stream that ends must not leave the hub growing."""
    hub = ProgressHub()
    document_id = uuid4()

    async with hub.subscribe(document_id):
        assert hub.subscriber_count == 1

    assert hub.subscriber_count == 0
    # And publishing to nobody is not an error.
    hub.publish(document_id)


async def test_publishing_to_an_unwatched_document_is_a_no_op():
    ProgressHub().publish(uuid4())


def test_dsn_drops_the_sqlalchemy_driver():
    """asyncpg.connect does not understand SQLAlchemy's `+driver` spelling."""
    assert (
        asyncpg_dsn("postgresql+asyncpg://app_rw:pw@db:5432/appdb")
        == "postgresql://app_rw:pw@db:5432/appdb"
    )
