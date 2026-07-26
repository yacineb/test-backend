"""PartnerJobSink adapters.

`DbPartnerJobSink` is the real one, and it does what the stub below promised
whoever landed the pipeline would do: resolve partner_job_id to a document,
raise UnknownPartnerJob when nothing is waiting, apply the outcome idempotently.

Resolution runs on the **system session**, not a tenant session, and that is not
a shortcut. The partner knows nothing about our organizations - `job_id` is the
only join key it can offer - so there is no org to scope the lookup to before
the row is found. The same reasoning already puts "find a user by email" on the
system session during login. The organization is then *derived* from the row
that comes back; nothing in the payload can name a tenant.
"""

from collections import deque

from app.application.partner import is_duplicate, outcome_status
from app.domain.document import DocumentStatus
from app.domain.errors import UnknownPartnerJob
from app.domain.partner import PartnerNotification
from app.infrastructure.db.repositories import UnscopedDocumentRepository
from app.infrastructure.db.session import Database


class DbPartnerJobSink:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def deliver(self, notification: PartnerNotification) -> None:
        async with self._database.system_session() as session:
            documents = UnscopedDocumentRepository(session)

            document = await documents.get_by_partner_job_id(notification.job_id)
            if document is None:
                # 404 rather than a silent accept: an unknown job_id is one we
                # never issued, and retrying the same bytes will not change it.
                raise UnknownPartnerJob(f"no document waiting on {notification.job_id}")

            if is_duplicate(document, notification):
                return

            if outcome_status(notification) is DocumentStatus.READY:
                await documents.complete(document.id)
            else:
                await documents.fail(document.id)

            await session.commit()


class InMemoryPartnerJobSink:
    """Accepts every job_id and remembers the last few notifications.

    Kept for the route-level tests, which are about the signature gate and the
    status codes rather than about persistence. Process-local, so it is fine
    for tests and was never fit for anything else.
    """

    def __init__(self, maxlen: int = 100) -> None:
        self.received: deque[PartnerNotification] = deque(maxlen=maxlen)

    async def deliver(self, notification: PartnerNotification) -> None:
        self.received.append(notification)
