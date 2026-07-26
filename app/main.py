from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from pydantic import BaseModel

from app.api.errors import register_error_handlers
from app.api.middleware import MaxBodySizeMiddleware
from app.api.routers import auth, documents, me, webhooks
from app.api.security_headers import SecurityHeadersMiddleware
from app.config import get_settings
from app.infrastructure.db.session import Database
from app.infrastructure.storage.posix import PosixObjectStore


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

    try:
        yield
    finally:
        await database.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Document processing API",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Must sit ahead of the multipart parser, which buffers file parts to disk
    # before any endpoint code runs. See app/api/middleware.py.
    app.add_middleware(
        MaxBodySizeMiddleware, max_bytes=get_settings().storage.max_body_bytes
    )
    # Added last, so it wraps outermost and also decorates the 413 that
    # MaxBodySizeMiddleware returns without ever reaching a route.
    app.add_middleware(SecurityHeadersMiddleware)
    register_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(documents.router)
    app.include_router(webhooks.router)

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    def health() -> Health:
        return Health(status="ok")

    return app


# Module-level instance kept so `app.main:app` and the existing health test
# keep working unchanged.
app = create_app()
