"""ASGI application boundary and lifecycle for the CEHO hot server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from ceho.hot.config import HotServerSettings
from ceho.hot.outbox import (
    OUTBOX_POLL_INTERVAL_SECONDS,
    DatabaseOutboxStore,
    EventPublisher,
    publish_pending,
    publisher_from_url,
)
from ceho.hot.session import create_database_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Drain pending outbox events when a database engine is configured."""
    task: asyncio.Task[None] | None = None
    if app.state.engine is not None:
        publisher = app.state.publisher or publisher_from_url(
            app.state.settings.event_publisher_url
        )
        app.state.publisher = publisher
        task = asyncio.create_task(_drain_outbox(app.state.engine, publisher))
    logger.info("CEHO hot server started")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        logger.info("CEHO hot server stopped")


async def _drain_outbox(engine: Engine, publisher: EventPublisher) -> None:
    while True:
        try:
            await asyncio.to_thread(_publish_with_engine, engine, publisher)
        except Exception:
            logger.exception("outbox drain failed")
        await asyncio.sleep(OUTBOX_POLL_INTERVAL_SECONDS)


def _publish_with_engine(engine: Engine, publisher: EventPublisher) -> None:
    with engine.connect() as connection:
        publish_pending(DatabaseOutboxStore(connection), publisher)


def create_app(
    settings: HotServerSettings | None = None,
    *,
    engine: Engine | None = None,
    publisher: EventPublisher | None = None,
) -> FastAPI:
    """Create a configured CEHO hot-server ASGI application."""
    active_settings = settings or HotServerSettings.from_environment()
    app = FastAPI(title="CEHO Hot Server", lifespan=_lifespan)
    app.state.settings = active_settings
    app.state.engine = engine
    app.state.publisher = publisher

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Report that the configured application can serve requests."""
        return {"status": "ok"}

    return app


def main() -> None:
    """Run the hot server with settings from the process environment."""
    settings = HotServerSettings.from_environment()
    engine = create_database_engine(settings.database_url)
    publisher = publisher_from_url(settings.event_publisher_url)
    uvicorn.run(
        create_app(settings, engine=engine, publisher=publisher),
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
