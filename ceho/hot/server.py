"""ASGI application boundary and lifecycle for the CEHO hot server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Annotated, TypeVar

import uvicorn
from fastapi import FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from ceho.hot.auth import (
    AuthenticationError,
    ControlCredential,
    ControlPrincipal,
    DatabaseLoginAccounts,
    LoginAccounts,
    LoginUnavailable,
    authenticate,
    read_control_credential,
)
from ceho.hot.config import ConfigurationError, HotServerSettings
from ceho.hot.outbox import (
    OUTBOX_POLL_INTERVAL_SECONDS,
    DatabaseOutboxStore,
    EventPublisher,
    publish_pending,
    publisher_from_url,
)
from ceho.hot.session import (
    ControlError,
    ControlStore,
    IssuedSession,
    PresenceBook,
    SessionView,
    SqlControlStore,
    create_database_engine,
    create_session,
    end_idle_sessions,
    end_session,
    join_session,
)
from ceho.logging import configure, get_logger

logger = get_logger(__name__)

SWEEP_INTERVAL_SECONDS = 30.0
T = TypeVar("T")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Drain pending outbox events when a database engine is configured."""
    tasks: list[asyncio.Task[None]] = []
    if app.state.engine is not None:
        publisher = app.state.publisher or publisher_from_url(
            app.state.settings.event_publisher_url,
            app.state.engine,
        )
        app.state.publisher = publisher
        tasks.append(asyncio.create_task(_drain_outbox(app.state.engine, publisher)))
        if app.state.control_store is None:
            tasks.append(asyncio.create_task(_idle_loop(app)))
    logger.info(
        "hot server started",
        host=app.state.settings.host,
        port=app.state.settings.port,
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
    login_accounts: LoginAccounts | None = None,
    control_store: ControlStore | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Create a configured CEHO hot-server ASGI application."""
    active_settings = settings or HotServerSettings.from_environment()
    app = FastAPI(title="CEHO Hot Server", lifespan=_lifespan)
    app.state.settings = active_settings
    app.state.engine = engine
    app.state.publisher = publisher
    app.state.login_accounts = login_accounts
    app.state.control_store = control_store
    app.state.presence = PresenceBook()
    app.state.now = now or (lambda: datetime.now(timezone.utc))

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Report that the configured application can serve requests."""
        return {"status": "ok"}

    @app.post("/login")
    async def login(body: _LoginBody) -> _LoginResponse:
        """Prove a platform password and return a control credential."""
        try:
            issued = await asyncio.to_thread(_perform_login, app, body.email, body.password)
        except AuthenticationError:
            raise HTTPException(status_code=401, detail="login rejected") from None
        except LoginUnavailable:
            raise HTTPException(status_code=503, detail="login is unavailable") from None
        except Exception:
            logger.exception("login failed")
            raise HTTPException(status_code=503, detail="login is unavailable") from None
        return _LoginResponse(
            credential=issued.credential,
            expires_at=issued.expires_at,
            user_id=issued.user_id,
            tenant_id=issued.tenant_id,
        )

    @app.post("/sessions", status_code=201)
    async def open_session(body: _CreateSessionBody, request: Request) -> _SessionOpened:
        """Create an active session and a signaling credential."""
        principal = _principal(request)
        issued = await _run(
            lambda: _open_session(request.app, principal, body),
        )
        return _opened(issued)

    @app.post("/sessions/{session_id}/join")
    async def join(
        session_id: Annotated[int, Path(gt=0)],
        request: Request,
    ) -> _SessionOpened:
        """Rejoin an active session and mint a new signaling credential."""
        principal = _principal(request)
        issued = await _run(
            lambda: _join_session(request.app, principal, session_id),
        )
        return _opened(issued)

    @app.post("/sessions/{session_id}/end")
    async def end(
        session_id: Annotated[int, Path(gt=0)],
        request: Request,
    ) -> _SessionEnded:
        """End an active session in the caller's tenant."""
        principal = _principal(request)
        ended = await _run(lambda: _end_session(request.app, principal, session_id))
        return _SessionEnded(session_id=ended.session_id, status=ended.status)

    return app


class _LoginBody(BaseModel):
    email: str
    password: str


class _CreateSessionBody(BaseModel):
    """Channel selection for a new session. The tenant comes from the control credential."""

    model_config = ConfigDict(extra="ignore")

    channel_id: int = Field(gt=0)
    description: str | None = None


class _SessionOpened(BaseModel):
    """Session id and the signaling credential for its connection."""

    session_id: int
    signaling_credential: str
    expires_at: datetime


class _SessionEnded(BaseModel):
    """A session that the control API has ended."""

    session_id: int
    status: str


class _ControlUnavailable(Exception):
    """Session control has no store and no database."""


class _LoginResponse(BaseModel):
    credential: str
    expires_at: datetime
    user_id: int
    tenant_id: int


def _perform_login(app: FastAPI, email: str, password: str) -> ControlCredential:
    settings = app.state.settings
    moment = app.state.now()
    accounts = app.state.login_accounts
    if accounts is None:
        if app.state.engine is None:
            raise LoginUnavailable("login is unavailable")
        with app.state.engine.connect() as connection:
            return authenticate(
                DatabaseLoginAccounts(connection),
                email,
                password,
                secret=settings.control_secret,
                now=moment,
                ttl_seconds=settings.control_credential_ttl_seconds,
            )
    return authenticate(
        accounts,
        email,
        password,
        secret=settings.control_secret,
        now=moment,
        ttl_seconds=settings.control_credential_ttl_seconds,
    )


def _principal(request: Request) -> ControlPrincipal:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="control credential is missing")
    try:
        return read_control_credential(
            token.strip(),
            request.app.state.settings.control_secret,
            now=request.app.state.now(),
        )
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from None


async def _run(action: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(action)
    except ControlError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from None
    except _ControlUnavailable:
        raise HTTPException(status_code=503, detail="session control is unavailable") from None
    except Exception:
        logger.exception("session request failed")
        raise HTTPException(status_code=500, detail="session request failed") from None


def _open_session(
    app: FastAPI,
    principal: ControlPrincipal,
    body: _CreateSessionBody,
) -> IssuedSession:
    with _control_store(app) as store:
        return create_session(
            store,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            channel_id=body.channel_id,
            description=body.description,
            now=app.state.now(),
            secret=app.state.settings.signaling_secret,
            ttl_seconds=app.state.settings.signaling_credential_ttl_seconds,
            publisher=app.state.publisher,
        )


def _join_session(app: FastAPI, principal: ControlPrincipal, session_id: int) -> IssuedSession:
    with _control_store(app) as store:
        return join_session(
            store,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_id=session_id,
            now=app.state.now(),
            secret=app.state.settings.signaling_secret,
            ttl_seconds=app.state.settings.signaling_credential_ttl_seconds,
        )


def _end_session(app: FastAPI, principal: ControlPrincipal, session_id: int) -> SessionView:
    with _control_store(app) as store:
        return end_session(
            store,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_id=session_id,
            now=app.state.now(),
            reason="explicit",
            publisher=app.state.publisher,
        )


def _opened(issued: IssuedSession) -> _SessionOpened:
    return _SessionOpened(
        session_id=issued.session.session_id,
        signaling_credential=issued.signaling_credential,
        expires_at=issued.expires_at,
    )


@contextmanager
def _control_store(app: FastAPI) -> Iterator[ControlStore]:
    bound = app.state.control_store
    if bound is not None:
        yield bound
        return
    engine = app.state.engine
    if engine is None:
        raise _ControlUnavailable("session control is unavailable")
    with engine.connect() as connection:
        yield SqlControlStore(connection)


async def _idle_loop(app: FastAPI) -> None:
    started = app.state.now()
    grant_unseen = True
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            ended = await asyncio.to_thread(_sweep_once, app, grant_unseen, started)
        except Exception:
            logger.warning("session idle sweep failed", exc_info=True)
            continue
        logger.debug("session idle sweep finished", ended=ended)
        grant_unseen = False


def _sweep_once(app: FastAPI, grant_unseen: bool, process_started_at: datetime) -> int:
    with _control_store(app) as store:
        ended = end_idle_sessions(
            store,
            app.state.presence,
            idle_seconds=app.state.settings.session_idle_timeout_seconds,
            now=app.state.now(),
            publisher=app.state.publisher,
            grant_unseen=grant_unseen,
            process_started_at=process_started_at,
        )
    return len(ended)


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
