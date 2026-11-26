"""Signaling offer route and the ICE servers returned at join."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ceho.hot.server import create_app
from ceho.hot.signaling import issue_signaling_credential
from ceho.hot.transport import InboundAudio
from ceho.hot.turn import IceRelay, TurnUnavailable
from ceho.hot.webrtc import WebRTCPeer
from tests.hot.conftest import MemoryControlStore, hot_settings
from tests.hot.test_session_api import CONTROL_SECRET, NOW, SIGNALING_SECRET, _bearer, _credential

STUN = "stun:stun.cloudflare.com:3478"
TURN = "turns:turn.cloudflare.com:443?transport=tcp"
OFFER = "\r\n".join(
    [
        "v=0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=sendonly",
        "a=rtpmap:111 opus/48000/2",
        "",
    ]
)
SECOND_MLINE = "\r\n".join(
    [
        "v=0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=sendonly",
        "a=rtpmap:111 opus/48000/2",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "a=rtpmap:96 VP8/90000",
        "",
    ]
)
RECVONLY = "\r\n".join(
    [
        "v=0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=recvonly",
        "a=rtpmap:111 opus/48000/2",
        "",
    ]
)
ANSWER = "\r\n".join(
    [
        "v=0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=candidate:1 1 UDP 33554431 203.0.113.8 443 typ relay",
        "",
    ]
)


class _Turn:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.ttls: list[int] = []

    def mint(self, *, ttl_seconds: int) -> IceRelay:
        self.ttls.append(ttl_seconds)
        if self.fail:
            raise TurnUnavailable("turn is unavailable")
        call = len(self.ttls)
        return IceRelay(
            stun_url=STUN,
            turn_url=TURN,
            username=f"turn-user-{call}",
            password=f"turn-pass-{call}",
        )


class _Peer:
    def __init__(self, relay: IceRelay) -> None:
        self.relay = relay
        self.closed = False
        self.offer: str | None = None
        self._stop = asyncio.Event()

    async def negotiate(self, offer_sdp: str) -> str:
        self.offer = offer_sdp
        return ANSWER

    def frames(self) -> AsyncIterator[InboundAudio]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[InboundAudio]:
        await self._stop.wait()
        while False:
            yield InboundAudio(
                timestamp_ms=0,
                duration_ms=20,
                payload=b"\x00\x00",
                codec="pcm_s16le",
                sample_rate=48_000,
                channel_count=1,
            )

    async def close(self) -> None:
        self.closed = True
        self._stop.set()


class _Peers:
    def __init__(self) -> None:
        self.built: list[_Peer] = []

    def __call__(self, relay: IceRelay) -> WebRTCPeer:
        peer = _Peer(relay)
        self.built.append(peer)
        return peer


def _signaling(
    *,
    tenant_id: int = 4,
    session_id: int,
    channel_tenant_id: int = 4,
    channel_id: int = 2,
    now: datetime = NOW,
    ttl_seconds: int = 60,
) -> str:
    credential, _expires_at = issue_signaling_credential(
        SIGNALING_SECRET,
        tenant_id=tenant_id,
        session_id=session_id,
        channel_tenant_id=channel_tenant_id,
        channel_id=channel_id,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    return credential


def _open(store: MemoryControlStore, turn: _Turn, peers: _Peers) -> FastAPI:
    return create_app(
        hot_settings(),
        control_store=store,
        now=lambda: NOW,
        turn=turn,
        peer_factory=peers,
    )


def _session(client: TestClient) -> int:
    created = client.post(
        "/sessions",
        headers=_bearer(_credential()),
        json={"channel_id": 2},
    )
    assert created.status_code == 201
    return int(created.json()["session_id"])


def test_join_returns_short_lived_cloudflare_ice_servers() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    turn = _Turn()

    app = _open(store, turn, _Peers())
    with TestClient(app) as client:
        created = client.post(
            "/sessions",
            headers=_bearer(_credential()),
            json={"channel_id": 2},
        )
        session_id = created.json()["session_id"]
        joined = client.post(f"/sessions/{session_id}/join", headers=_bearer(_credential()))

    assert created.status_code == 201
    assert joined.status_code == 200
    for body in (created.json(), joined.json()):
        assert body["stun_url"] == STUN
        assert body["turn_url"] == TURN
        assert body["turn_username"].startswith("turn-user-")
        assert body["turn_password"].startswith("turn-pass-")
        assert body["turn_password"] != CONTROL_SECRET
    assert created.json()["turn_password"] != joined.json()["turn_password"]
    assert turn.ttls == [60, 60]


def test_expired_credential_rejects_the_offer_and_leaves_no_peer() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    peers = _Peers()

    app = _open(store, _Turn(), peers)
    with TestClient(app) as client:
        session_id = _session(client)
        credential = _signaling(
            session_id=session_id,
            now=NOW - timedelta(seconds=120),
            ttl_seconds=60,
        )
        response = client.post(
            f"/sessions/{session_id}/offer",
            headers=_bearer(credential),
            json={"sdp": OFFER},
        )
        running = app.state.peers

    assert response.status_code == 401
    assert "expired" in response.json()["detail"]
    assert peers.built == []
    assert running == {}


def test_cross_session_credential_rejects_the_offer_and_leaves_no_peer() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    peers = _Peers()

    app = _open(store, _Turn(), peers)
    with TestClient(app) as client:
        session_id = _session(client)
        credential = _signaling(session_id=session_id + 1)
        response = client.post(
            f"/sessions/{session_id}/offer",
            headers=_bearer(credential),
            json={"sdp": OFFER},
        )

    assert response.status_code == 401
    assert "session" in response.json()["detail"]
    assert peers.built == []
    assert app.state.peers == {}


def test_cross_tenant_credential_rejects_the_offer_and_leaves_no_peer() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    peers = _Peers()

    app = _open(store, _Turn(), peers)
    with TestClient(app) as client:
        session_id = _session(client)
        credential = _signaling(tenant_id=5, session_id=session_id)
        response = client.post(
            f"/sessions/{session_id}/offer",
            headers=_bearer(credential),
            json={"sdp": OFFER},
        )

    assert response.status_code == 404
    assert peers.built == []
    assert app.state.peers == {}


@pytest.mark.parametrize("sdp", [SECOND_MLINE, RECVONLY])
def test_a_rejected_offer_matches_the_adapter_and_closes_the_peer(sdp: str) -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    peers = _Peers()

    app = _open(store, _Turn(), peers)
    with TestClient(app) as client:
        session_id = _session(client)
        credential = _signaling(session_id=session_id)
        response = client.post(
            f"/sessions/{session_id}/offer",
            headers=_bearer(credential),
            json={"sdp": sdp},
        )
        presence = app.state.presence.peer_count(4, session_id)

    assert response.status_code == 400
    assert peers.built[0].closed is True
    assert app.state.peers == {}
    assert presence == 0


def test_one_offer_returns_an_answer_and_establishes_the_microphone() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    peers = _Peers()

    app = _open(store, _Turn(), peers)
    with TestClient(app) as client:
        session_id = _session(client)
        credential = _signaling(session_id=session_id)
        response = client.post(
            f"/sessions/{session_id}/offer",
            headers=_bearer(credential),
            json={"sdp": OFFER},
        )
        live = app.state.peers[(4, session_id)]
        answer = response.json()["sdp"]
        assert live.adapter.state is not None
        assert live.adapter.state.value == "connected"
        assert app.state.presence.peer_count(4, session_id) == 1

    assert response.status_code == 200
    assert response.json()["type"] == "answer"
    assert "typ relay" in answer or "typ srflx" in answer
    assert live.adapter.source_id.endswith("/microphone")
    assert peers.built[0].relay.stun_url == STUN
    assert peers.built[0].relay.turn_url == TURN
    assert peers.built[0].relay.password == "turn-pass-2"
    assert peers.built[0].offer == OFFER
    assert app.state.presence.peer_count(4, session_id) == 0


def test_offer_logs_do_not_carry_the_turn_password_or_the_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    caplog.set_level("DEBUG", logger="ceho.hot.server")

    app = _open(store, _Turn(), _Peers())
    with TestClient(app) as client:
        session_id = _session(client)
        credential = _signaling(session_id=session_id)
        response = client.post(
            f"/sessions/{session_id}/offer",
            headers=_bearer(credential),
            json={"sdp": OFFER},
        )

    assert response.status_code == 200
    rendered = " ".join(
        [
            *(record.getMessage() for record in caplog.records),
            *(
                str(value)
                for record in caplog.records
                for value in getattr(record, "ceho_fields", {}).values()
            ),
        ]
    )
    assert credential not in rendered
    assert "turn-pass-1" not in rendered
    assert "turn-pass-2" not in rendered
    assert OFFER not in rendered
    assert any(record.msg == "offer accepted" for record in caplog.records)
