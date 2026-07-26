from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.errors import register_error_handlers
from app.api.routers import auth, me, webhooks
from app.config import get_settings
from app.infrastructure.db.session import Database


class Health(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = Database(get_settings())
    app.state.database = database
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
    register_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(webhooks.router)

    @app.get("/health", tags=["ops"], summary="Liveness probe")
    def health() -> Health:
        return Health(status="ok")

    return app


# Module-level instance kept so `app.main:app` and the existing health test
# keep working unchanged.
app = create_app()
