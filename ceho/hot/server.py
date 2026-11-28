"""ASGI application boundary and lifecycle for the CEHO hot server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, TypeVar
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Path, Request
from pipecat.processors.frame_processor import FrameProcessor
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
from ceho.hot.config import ConfigurationError, HotServerSettings, turn_credential_pair
from ceho.hot.media import (
    DatabaseMediaCatalog,
    DirectSourceRecording,
    MediaProvenance,
    s3_object_storage,
)
from ceho.hot.microphone import (
    MicrophoneRecorder,
    TranscriptCommit,
    attach_microphone,
    direct_source_processors,
    recording_source_id,
)
from ceho.hot.outbox import (
    OUTBOX_POLL_INTERVAL_SECONDS,
    DatabaseOutboxStore,
    EventPublisher,
    publish_pending,
    publisher_from_url,
)
from ceho.hot.pipeline import TranscriptCandidate
from ceho.hot.session import (
    ControlError,
    ControlStore,
    FinalizeTranscript,
    IssuedSession,
    PresenceBook,
    SessionView,
    SqlControlStore,
    create_database_engine,
    create_session,
    end_idle_sessions,
    end_session,
    finalize_transcript,
    join_session,
)
from ceho.hot.signaling import (
    SignalingCredentialError,
    SignalingGrant,
    authorize_signaling,
    read_signaling_credential,
)
from ceho.hot.transport import AuthorizedSessionBinding, SignalingError, TransportError
from ceho.hot.turn import CloudflareTurn, IceRelay, StunOnlyTurn, TurnMint, TurnUnavailable
from ceho.hot.webrtc import DirectSourceWebRTC, WebRTCPeer, ice_servers_for
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
        live = list(app.state.peers.values())
        for peer in live:
            await peer.adapter.close()
        watches = [peer.watch for peer in live if peer.watch is not None]
        if watches:
            await asyncio.gather(*watches, return_exceptions=True)
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
    turn: TurnMint | None = None,
    peer_factory: Callable[[IceRelay], WebRTCPeer] | None = None,
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
    app.state.turn = turn or _turn_from_settings(active_settings)
    app.state.peer_factory = peer_factory or _peer_with_relay
    app.state.peers = {}
    app.state.next_peer = 1
    app.state.peer_lock = asyncio.Lock()
    app.state.object_storage = None
    app.state.recorder_factory = None
    app.state.transcript_commit = None
    app.state.microphone_processors = None

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
        return await _opened(request.app, issued)

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
        return await _opened(request.app, issued)

    @app.post("/sessions/{session_id}/end")
    async def end(
        session_id: Annotated[int, Path(gt=0)],
        request: Request,
    ) -> _SessionEnded:
        """End an active session in the caller's tenant."""
        principal = _principal(request)
        ended = await _run(lambda: _end_session(request.app, principal, session_id))
        return _SessionEnded(session_id=ended.session_id, status=ended.status)

    @app.post("/sessions/{session_id}/offer")
    async def offer(
        session_id: Annotated[int, Path(gt=0)],
        body: _OfferBody,
        request: Request,
    ) -> _Answer:
        """Accept one microphone offer and return the SDP answer."""
        grant = _signaling_grant(request)
        try:
            authorize_signaling(
                grant,
                tenant_id=grant.tenant_id,
                session_id=session_id,
                channel_tenant_id=grant.channel_tenant_id,
                channel_id=grant.channel_id,
            )
        except SignalingCredentialError as error:
            raise HTTPException(status_code=401, detail=str(error)) from None
        session = await _run(
            lambda: _load_session(request.app, grant.tenant_id, session_id),
        )
        if session is None:
            logger.error(
                "signaling rejected",
                reason="session_missing",
                tenant_id=grant.tenant_id,
                session_id=session_id,
            )
            raise HTTPException(status_code=404, detail="session does not exist")
        if session.status != "active":
            logger.error(
                "signaling rejected",
                reason="session_ended",
                tenant_id=grant.tenant_id,
                session_id=session_id,
            )
            raise HTTPException(status_code=409, detail="session has ended")
        logger.debug(
            "offer started",
            tenant_id=grant.tenant_id,
            session_id=session_id,
        )
        relay = await _mint(request.app)
        answer = await _accept_offer(
            request.app,
            grant,
            relay,
            body.sdp,
            session.started_at,
        )
        return _Answer(sdp=answer, type="answer")

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
    """Session id, signaling credential, and the ICE servers for that join."""

    session_id: int
    signaling_credential: str
    expires_at: datetime
    stun_url: str
    turn_url: str
    turn_username: str
    turn_password: str


class _OfferBody(BaseModel):
    """One browser offer. The server does not interpret the SDP."""

    model_config = ConfigDict(extra="ignore")

    sdp: str


class _Answer(BaseModel):
    """The SDP answer for the one accepted offer."""

    sdp: str
    type: str


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


@dataclass
class _LivePeer:
    adapter: DirectSourceWebRTC
    generation: int
    tenant_id: int
    session_id: int
    started_at: datetime
    watch: asyncio.Task[None] | None = None


def _turn_from_settings(settings: HotServerSettings) -> TurnMint:
    pair = turn_credential_pair(settings.turn_key_id, settings.turn_api_token)
    if pair is None:
        logger.info("turn not configured", stun_url=settings.stun_url)
        return StunOnlyTurn(settings.stun_url)
    key_id, api_token = pair
    return CloudflareTurn(
        key_id,
        api_token,
        stun_url=settings.stun_url,
        turn_url=settings.turn_url,
    )


def _peer_with_relay(relay: IceRelay) -> WebRTCPeer:
    from ceho.hot.webrtc import SmallWebRTCPeer

    return SmallWebRTCPeer(ice_servers=ice_servers_for(relay))


def _signaling_grant(request: Request) -> SignalingGrant:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="signaling credential is missing")
    try:
        return read_signaling_credential(
            token.strip(),
            request.app.state.settings.signaling_secret,
            now=request.app.state.now(),
        )
    except SignalingCredentialError as error:
        raise HTTPException(status_code=401, detail=str(error)) from None


def _load_session(app: FastAPI, tenant_id: int, session_id: int) -> SessionView | None:
    with _control_store(app) as store:
        return store.get_session(tenant_id, session_id)


async def _mint(app: FastAPI) -> IceRelay:
    try:
        return await asyncio.to_thread(
            lambda: app.state.turn.mint(ttl_seconds=app.state.settings.turn_ttl_seconds)
        )
    except TurnUnavailable:
        raise HTTPException(status_code=503, detail="turn is unavailable") from None
    except Exception:
        logger.exception("turn credentials failed")
        raise HTTPException(status_code=503, detail="turn is unavailable") from None


async def _accept_offer(
    app: FastAPI,
    grant: SignalingGrant,
    relay: IceRelay,
    sdp: str,
    started_at: datetime,
) -> str:
    key = (grant.tenant_id, grant.session_id)
    previous = app.state.peers.get(key)
    if previous is not None:
        await previous.adapter.close()
        await _release_peer(app, key, previous.generation)
    peer_id = uuid4().hex
    adapter = DirectSourceWebRTC(
        AuthorizedSessionBinding(
            tenant_id=grant.tenant_id,
            session_id=grant.session_id,
            peer_id=peer_id,
            expires_at=grant.expires_at,
        ),
        app.state.peer_factory(relay),
        now=app.state.now,
    )
    try:
        answer = await adapter.accept_offer(sdp)
    except SignalingError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except TransportError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    generation = app.state.next_peer
    app.state.next_peer += 1
    live = _LivePeer(
        adapter=adapter,
        generation=generation,
        tenant_id=grant.tenant_id,
        session_id=grant.session_id,
        started_at=started_at,
    )
    app.state.peers[key] = live
    app.state.presence.attach(grant.tenant_id, grant.session_id)
    live.watch = asyncio.create_task(
        _watch_peer(app, key, generation, adapter, started_at),
        name=f"peer-{peer_id}",
    )
    logger.info(
        "offer accepted",
        tenant_id=grant.tenant_id,
        session_id=grant.session_id,
        peer_id=peer_id,
        source_id=adapter.source_id,
    )
    return answer


async def _watch_peer(
    app: FastAPI,
    key: tuple[int, int],
    generation: int,
    adapter: DirectSourceWebRTC,
    started_at: datetime,
) -> None:
    try:
        await _serve_microphone(
            app,
            adapter,
            tenant_id=key[0],
            session_id=key[1],
            started_at=started_at,
        )
    except Exception:
        logger.exception(
            "microphone failed",
            tenant_id=key[0],
            session_id=key[1],
            source_id=adapter.source_id,
        )
        await adapter.close()
    finally:
        await _release_peer(app, key, generation)


async def _serve_microphone(
    app: FastAPI,
    adapter: DirectSourceWebRTC,
    *,
    tenant_id: int,
    session_id: int,
    started_at: datetime,
) -> None:
    """Record and transcribe the peer's microphone until it closes.

    With no database and no injected recorder, events are drained so the
    peer can finish. That path does not open storage or a pipeline run.
    """
    recorder = _microphone_recorder(
        app,
        tenant_id=tenant_id,
        session_id=session_id,
        source_id=adapter.source_id,
        started_at=started_at,
    )
    processors = _microphone_processors(app)
    if recorder is None and not processors:
        async for _event in adapter.events():
            pass
        return
    if recorder is None:
        logger.error(
            "microphone recording failed",
            tenant_id=tenant_id,
            session_id=session_id,
            source_id=adapter.source_id,
            error="recording is unavailable",
        )
        async for _event in adapter.events():
            pass
        return
    await attach_microphone(
        adapter,
        processors=processors,
        recorder=recorder,
        commit=_microphone_commit(app),
    )


def _microphone_processors(app: FastAPI) -> Sequence[FrameProcessor]:
    supplied = app.state.microphone_processors
    if supplied is not None:
        return supplied
    return direct_source_processors(app.state.settings.deepgram_api_key)


def _microphone_recorder(
    app: FastAPI,
    *,
    tenant_id: int,
    session_id: int,
    source_id: str,
    started_at: datetime,
) -> MicrophoneRecorder | None:
    factory = app.state.recorder_factory
    if factory is not None:
        return factory(tenant_id, session_id, source_id, started_at)
    if app.state.engine is None:
        return None
    storage = app.state.object_storage
    if storage is None:
        storage = s3_object_storage(app.state.settings)
        app.state.object_storage = storage
    return DirectSourceRecording(
        tenant_id=tenant_id,
        session_id=session_id,
        source_id=recording_source_id(source_id),
        session_started_at=started_at,
        storage=storage,
        catalog=DatabaseMediaCatalog(app.state.engine),
    )


def _microphone_commit(app: FastAPI) -> TranscriptCommit:
    supplied = app.state.transcript_commit
    if supplied is not None:
        return supplied
    return _transcript_commit(app)


def _transcript_commit(app: FastAPI) -> TranscriptCommit:
    def commit(
        candidate: TranscriptCandidate,
        *,
        session_speaker_id: int | None,
        provenance: MediaProvenance | None,
    ) -> int:
        if app.state.engine is None:
            raise RuntimeError("transcript commit is unavailable")
        command = FinalizeTranscript(
            tenant_id=candidate.context.tenant_id,
            session_id=candidate.context.session_id,
            text=candidate.text,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            session_speaker_id=session_speaker_id,
            label=candidate.speaker_label,
            media_object_id=None if provenance is None else provenance.media_object_id,
            media_start_offset_ms=None if provenance is None else provenance.media_start_offset_ms,
            media_end_offset_ms=None if provenance is None else provenance.media_end_offset_ms,
        )
        with app.state.engine.connect() as connection:
            finalized = finalize_transcript(connection, command, app.state.publisher)
        return finalized.session_speaker_id

    return commit


async def _release_peer(app: FastAPI, key: tuple[int, int], generation: int) -> None:
    async with app.state.peer_lock:
        live = app.state.peers.get(key)
        if live is None or live.generation != generation:
            return
        del app.state.peers[key]
        app.state.presence.detach(live.tenant_id, live.session_id, app.state.now())


async def _opened(app: FastAPI, issued: IssuedSession) -> _SessionOpened:
    relay = await _mint(app)
    return _SessionOpened(
        session_id=issued.session.session_id,
        signaling_credential=issued.signaling_credential,
        expires_at=issued.expires_at,
        stun_url=relay.stun_url,
        turn_url=relay.turn_url,
        turn_username=relay.username,
        turn_password=relay.password,
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
