"""ASGI application boundary and lifecycle for the CEHO hot server."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ceho.hot.config import HotServerSettings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Own the application lifecycle before hot-path services are added."""
    logger.info("CEHO hot server started")
    yield
    logger.info("CEHO hot server stopped")


def create_app(settings: HotServerSettings | None = None) -> FastAPI:
    """Create a configured CEHO hot-server ASGI application."""
    active_settings = settings or HotServerSettings.from_environment()
    app = FastAPI(title="CEHO Hot Server", lifespan=_lifespan)
    app.state.settings = active_settings

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Report that the configured application can serve requests."""
        return {"status": "ok"}

    return app


def main() -> None:
    """Run the hot server with settings from the process environment."""
    settings = HotServerSettings.from_environment()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
