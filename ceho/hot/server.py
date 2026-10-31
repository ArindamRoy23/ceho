"""ASGI application boundary and lifecycle for the CEHO hot server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, TypeVar

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from ceho.hot.config import HotServerSettings
from ceho.hot.credentials import (
    ClientPrincipal,
    InvalidCredential,
    verify_client_token,
)
from ceho.hot.session import (
    ControlError,
    ControlStore,
    PresenceBook,
    SessionView,
    SqlControlStore,
    create_session,
    end_idle_sessions,
    end_session,
    join_session,
)

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30
T = TypeVar("T")


class CreateSessionRequest(BaseModel):
    """Channel selection for a new session. The tenant comes from the client token."""

    model_config = ConfigDict(extra="ignore")

    channel_id: int = Field(gt=0)
    description: str | None = None


class SessionOpened(BaseModel):
    """Session id and the signaling credential for its connection."""

    session_id: int
    signaling_token: str
    expires_at: datetime


class SessionEnded(BaseModel):
    """A session that the control API has ended."""

    session_id: int
    status: str


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Sweep idle sessions in production. An injected store keeps tests offline."""
    logger.info("CEHO hot server started")
    sweep: asyncio.Task[None] | None = None
    if app.state.control_store is None:
        sweep = asyncio.create_task(_idle_loop(app))
    try:
        yield
    finally:
        if sweep is not None:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
        engine = app.state.engine
        if engine is not None:
            engine.dispose()
        logger.info("CEHO hot server stopped")


def create_app(
    settings: HotServerSettings | None = None,
    control_store: ControlStore | None = None,
) -> FastAPI:
    """Create a configured CEHO hot-server ASGI application."""
    active_settings = settings or HotServerSettings.from_environment()
    app = FastAPI(title="CEHO Hot Server", lifespan=_lifespan)
    app.state.settings = active_settings
    app.state.control_store = control_store
    app.state.presence = PresenceBook()
    app.state.engine = None
    app.state.session_factory = None

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Report that the configured application can serve requests."""
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    def open_session(
        body: CreateSessionRequest,
        principal: Annotated[ClientPrincipal, Depends(_require_client)],
        store: Annotated[ControlStore, Depends(_open_store)],
    ) -> SessionOpened:
        """Create an active session and a signaling credential."""
        issued = _call(
            lambda: create_session(
                store,
                tenant_id=principal.tenant_id,
                channel_id=body.channel_id,
                description=body.description,
                now=_now(),
                secret=app.state.settings.signaling_secret,
                ttl=_ttl(app),
            )
        )
        return _opened(issued.session, issued.signaling_token, issued.expires_at)

    @app.post("/sessions/{session_id}/join")
    def join(
        session_id: Annotated[int, Path(gt=0)],
        principal: Annotated[ClientPrincipal, Depends(_require_client)],
        store: Annotated[ControlStore, Depends(_open_store)],
    ) -> SessionOpened:
        """Rejoin an active session and mint a new signaling credential."""
        issued = _call(
            lambda: join_session(
                store,
                tenant_id=principal.tenant_id,
                session_id=session_id,
                now=_now(),
                secret=app.state.settings.signaling_secret,
                ttl=_ttl(app),
            )
        )
        return _opened(issued.session, issued.signaling_token, issued.expires_at)

    @app.post("/sessions/{session_id}/end")
    def end(
        session_id: Annotated[int, Path(gt=0)],
        principal: Annotated[ClientPrincipal, Depends(_require_client)],
        store: Annotated[ControlStore, Depends(_open_store)],
    ) -> SessionEnded:
        """End an active session in the caller's tenant."""
        ended = _call(
            lambda: end_session(
                store,
                tenant_id=principal.tenant_id,
                session_id=session_id,
                now=_now(),
                reason="explicit",
            )
        )
        return SessionEnded(session_id=ended.session_id, status=ended.status)

    return app


def main() -> None:
    """Run the hot server with settings from the process environment."""
    settings = HotServerSettings.from_environment()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


def _require_client(request: Request) -> ClientPrincipal:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="client credential is missing")
    try:
        return verify_client_token(
            token.strip(),
            request.app.state.settings.signaling_secret,
            _now(),
        )
    except InvalidCredential as error:
        raise HTTPException(status_code=401, detail=str(error)) from error


def _open_store(request: Request) -> Iterator[ControlStore]:
    bound = request.app.state.control_store
    if bound is not None:
        yield bound
        return
    database = _session_factory(request.app)()
    try:
        yield SqlControlStore(database)
    finally:
        database.close()


def _session_factory(app: FastAPI) -> sessionmaker[DbSession]:
    factory = app.state.session_factory
    if factory is not None:
        return factory
    engine = create_engine(_psycopg_url(app.state.settings.database_url))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.state.engine = engine
    app.state.session_factory = factory
    return factory


def _psycopg_url(database_url: str) -> str:
    """Use the installed psycopg driver for a plain postgresql URL."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def _call(action: Callable[[], T]) -> T:
    try:
        return action()
    except ControlError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


def _opened(session: SessionView, token: str, expires_at: datetime) -> SessionOpened:
    return SessionOpened(
        session_id=session.session_id,
        signaling_token=token,
        expires_at=expires_at,
    )


def _ttl(app: FastAPI) -> timedelta:
    return timedelta(seconds=app.state.settings.signaling_token_ttl_seconds)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _idle_loop(app: FastAPI) -> None:
    started = _now()
    grant_unseen = True
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(_sweep_once, app, grant_unseen, started)
        except Exception:
            logger.exception("session idle sweep failed")
            continue
        grant_unseen = False


def _sweep_once(app: FastAPI, grant_unseen: bool, process_started_at: datetime) -> None:
    database = _session_factory(app)()
    try:
        end_idle_sessions(
            SqlControlStore(database),
            app.state.presence,
            idle_seconds=app.state.settings.session_idle_timeout_seconds,
            now=_now(),
            grant_unseen=grant_unseen,
            process_started_at=process_started_at,
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
