"""ASGI application boundary and lifecycle for the CEHO hot server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from ceho.hot.config import ConfigurationError, HotServerSettings
from ceho.hot.outbox import (
    OUTBOX_POLL_INTERVAL_SECONDS,
    DatabaseOutboxStore,
    EventPublisher,
    publish_pending,
    publisher_from_url,
)
from ceho.hot.session import create_database_engine
from ceho.logging import configure, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Drain pending outbox events when a database engine is configured."""
    task: asyncio.Task[None] | None = None
    if app.state.engine is not None:
        publisher = app.state.publisher or publisher_from_url(
            app.state.settings.event_publisher_url,
            app.state.engine,
        )
        app.state.publisher = publisher
        task = asyncio.create_task(_drain_outbox(app.state.engine, publisher))
    logger.info(
        "hot server started",
        host=app.state.settings.host,
        port=app.state.settings.port,
    )
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        logger.info(
            "hot server stopped",
            host=app.state.settings.host,
            port=app.state.settings.port,
        )


async def _drain_outbox(engine: Engine, publisher: EventPublisher) -> None:
    while True:
        try:
            published = await asyncio.to_thread(_publish_with_engine, engine, publisher)
        except Exception:
            logger.warning("outbox drain failed", exc_info=True)
        else:
            logger.debug("outbox drain finished", published=published)
        await asyncio.sleep(OUTBOX_POLL_INTERVAL_SECONDS)


def _publish_with_engine(engine: Engine, publisher: EventPublisher) -> int:
    with engine.connect() as connection:
        return publish_pending(DatabaseOutboxStore(connection), publisher)


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
    configure()
    try:
        settings = HotServerSettings.from_environment()
        engine = create_database_engine(settings.database_url)
        publisher = publisher_from_url(settings.event_publisher_url, engine)
    except ConfigurationError as error:
        logger.critical("hot server exiting", error=str(error))
        raise
    try:
        uvicorn.run(
            create_app(settings, engine=engine, publisher=publisher),
            host=settings.host,
            port=settings.port,
            log_config=None,
        )
    except OSError as error:
        logger.critical("hot server exiting", error=str(error))
        raise


if __name__ == "__main__":
    main()
