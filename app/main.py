import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from dbos import DBOS, DBOSConfig
from fastapi import FastAPI
from pydantic import BaseModel

from app.api.errors import register_error_handlers
from app.api.middleware import MaxBodySizeMiddleware
from app.api.request_context import RequestContextMiddleware
from app.api.routers import auth, documents, me, webhooks
from app.api.security_headers import SecurityHeadersMiddleware
from app.config import get_settings
from app.infrastructure.db.session import Database
from app.infrastructure.progress import (
    ProgressHub,
    ProgressListener,
    asyncpg_dsn,
)
from app.infrastructure.storage.posix import PosixObjectStore
from app.observability import configure_logging
from app.pipeline import runtime

logger = logging.getLogger(__name__)


class Health(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    database = Database(settings)
    app.state.database = database

    await anyio.to_thread.run_sync(
        lambda: settings.storage.root.mkdir(parents=True, exist_ok=True)
    )
    app.state.object_store = PosixObjectStore(
        settings.storage.root, settings.storage.chunk_bytes
    )

    # Pipeline workers have no request behind them, so they are handed the same
    # pool here rather than through a dependency. DBOS itself is launched by the
    # middleware installed in create_app, once startup completes.
    runtime.bind(database)

    # One LISTEN connection for this process, fanning out in memory to whatever
    # streams it happens to be serving. See app/infrastructure/progress.py.
    app.state.progress = ProgressHub()
    listener = ProgressListener(
        dsn=asyncpg_dsn(settings.db.url),
        channel=settings.progress.channel,
        hub=app.state.progress,
    )
    await listener.start()

    # The one startup line worth printing: it states the configuration that
    # decides where bytes go and how large they may be, which is the first thing
    # anyone asks when an upload behaves unexpectedly in an environment.
    logger.info(
        "app.started",
        extra={
            "storage_root": str(settings.storage.root),
            "max_upload_bytes": settings.storage.max_upload_bytes,
            "log_level": settings.logging.level,
        },
    )
    try:
        yield
    finally:
        logger.info("app.stopping")
        await listener.stop()
        await database.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.logging.level, settings.logging.format)

    app = FastAPI(
        title="Document processing API",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Must sit ahead of the multipart parser, which buffers file parts to disk
    # before any endpoint code runs. See app/api/middleware.py.
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.storage.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    # Added last, so it wraps outermost: every other middleware's output --
    # including the 413 that never reaches a route -- is emitted with a request
    # id bound and counted in the access log.
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(documents.router)
    app.include_router(webhooks.router)

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    def health() -> Health:
        return Health(status="ok")

    # Registers the pipeline's steps, workflows and queue, and installs the
    # middleware that launches DBOS on startup and destroys it on shutdown, so
    # there is no explicit DBOS.launch() to keep in sync with the lifespan.
    DBOS(
        config=DBOSConfig(
            name="primmo-pipeline",
            system_database_url=get_settings().pipeline.system_database_url,
        ),
        fastapi=app,
    )

    return app


# Module-level instance kept so `app.main:app` and the existing health test
# keep working unchanged.
app = create_app()
