"""Getting "document X changed" from the process that wrote it to the process
holding the client's connection.

The pipeline already produces the event stream as a side effect of doing its
job: every status change is a write, and migration 0004 turns every such write
into a `NOTIFY`. All that is left is one hop, and Postgres is already the
broker for it - no queue, no bus, no second source of truth.

Two properties keep this small:

* **The channel carries ids, not state.** Subscribers re-read the projection
  through their own RLS-scoped session, so a duplicated, coalesced or
  out-of-order notification is harmless, and no tenant data crosses a channel
  that every process is listening to.
* **One connection per process, not per subscriber.** A process holds a single
  dedicated `LISTEN` connection and fans out in memory. Five thousand watchers
  cost five thousand sockets and one database connection.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


class ProgressHub:
    """Fan-out from the listener to this process's subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[None]]] = {}

    @asynccontextmanager
    async def subscribe(self, document_id: UUID) -> AsyncIterator[asyncio.Queue[None]]:
        """A queue that yields once per change, coalesced.

        `maxsize=1` is the debouncer: a wake already pending means "re-read",
        and a second one would mean exactly the same thing. Dropping it needs
        no timer and cannot lose information, because the reader always fetches
        current state rather than a delta.
        """
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.setdefault(document_id, set()).add(queue)
        try:
            yield queue
        finally:
            watchers = self._subscribers.get(document_id)
            if watchers is not None:
                watchers.discard(queue)
                if not watchers:
                    del self._subscribers[document_id]

    def publish(self, document_id: UUID) -> None:
        for queue in self._subscribers.get(document_id, ()):
            self._wake(queue)

    def wake_everyone(self) -> None:
        """Used after the listener reconnects.

        Notifications that arrived while the connection was down are gone, so
        every subscriber re-reads and converges rather than waiting for a
        change that has already happened.
        """
        for watchers in self._subscribers.values():
            for queue in watchers:
                self._wake(queue)

    @property
    def subscriber_count(self) -> int:
        return sum(len(watchers) for watchers in self._subscribers.values())

    @staticmethod
    def _wake(queue: asyncio.Queue[None]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(None)


class ProgressListener:
    """Holds the process's `LISTEN` connection and feeds the hub.

    Runs its own supervision loop: if the connection drops, it reconnects and
    wakes every subscriber, because anything notified in the gap was missed.
    """

    def __init__(
        self, dsn: str, channel: str, hub: ProgressHub, retry_seconds: float = 1.0
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._hub = hub
        self._retry_seconds = retry_seconds
        self._task: asyncio.Task[None] | None = None
        self._connection: asyncpg.Connection | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="progress-listener")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close()

    def _on_notify(self, _connection, _pid, _channel, payload: str) -> None:
        try:
            document_id = UUID(payload)
        except ValueError:
            logger.warning("ignoring malformed progress payload: %r", payload)
            return
        self._hub.publish(document_id)

    async def _run(self) -> None:
        first = True
        while True:
            try:
                self._connection = await asyncpg.connect(self._dsn)
                await self._connection.add_listener(self._channel, self._on_notify)
                if not first:
                    # Reconnected: the gap swallowed notifications, so make
                    # every subscriber re-read instead of waiting for the next
                    # change that may never come.
                    self._hub.wake_everyone()
                first = False
                # Nothing to poll - asyncpg dispatches callbacks on its own
                # reader task. Sleep until cancelled or the connection dies.
                while not self._connection.is_closed():
                    await asyncio.sleep(self._retry_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("progress listener connection failed; retrying")
            finally:
                await self._close()
            await asyncio.sleep(self._retry_seconds)

    async def _close(self) -> None:
        if self._connection is not None and not self._connection.is_closed():
            with contextlib.suppress(Exception):
                await self._connection.close()
        self._connection = None


def asyncpg_dsn(url: str) -> str:
    """SQLAlchemy spells the driver in the URL; asyncpg.connect does not."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)
