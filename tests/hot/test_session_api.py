"""HTTP contract for session create, join, and end."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from ceho.hot.credentials import (
    authorize_signaling,
    issue_client_token,
    issue_signaling_token,
    signaling_grant,
)
from ceho.hot.server import create_app
from tests.hot.conftest import MemoryControlStore, hot_settings
from tests.hot.test_credentials import SECRET

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _client_token(
    tenant_id: int = 4,
    *,
    now: datetime | None = None,
    ttl: timedelta | None = None,
) -> str:
    return issue_client_token(
        SECRET,
        tenant_id,
        datetime.now(timezone.utc) if now is None else now,
        timedelta(minutes=5) if ttl is None else ttl,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_missing_client_credential_is_rejected() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)

    with TestClient(create_app(hot_settings(), store)) as client:
        response = client.post("/sessions", json={"channel_id": 2})

    assert response.status_code == 401
    assert response.json()["detail"] == "client credential is missing"


def test_signaling_credential_is_rejected_on_create() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token, _expires_at = issue_signaling_token(
        SECRET,
        tenant_id=4,
        session_id=1,
        channel_tenant_id=4,
        channel_id=2,
        now=datetime.now(timezone.utc),
        ttl=timedelta(seconds=60),
    )

    with TestClient(create_app(hot_settings(), store)) as client:
        response = client.post("/sessions", headers=_bearer(token), json={"channel_id": 2})

    assert response.status_code == 401
    assert store.sessions == {}


def test_expired_client_credential_is_rejected() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token = _client_token(now=NOW - timedelta(hours=1), ttl=timedelta(seconds=1))

    with TestClient(create_app(hot_settings(), store)) as client:
        response = client.post("/sessions", headers=_bearer(token), json={"channel_id": 2})

    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_create_join_and_end_stay_inside_the_authenticated_tenant() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    # The API verifies tokens against the current clock, so mint a live client token.
    token = issue_client_token(SECRET, 4, datetime.now(timezone.utc), timedelta(minutes=5))
    app = create_app(hot_settings(), store)

    with TestClient(app) as client:
        created = client.post(
            "/sessions",
            headers=_bearer(token),
            json={"channel_id": 2, "tenant_id": 9, "description": "planning"},
        )
        assert created.status_code == 201
        body = created.json()
        grant = signaling_grant(body["signaling_token"], SECRET, datetime.now(timezone.utc))
        authorize_signaling(
            grant,
            tenant_id=4,
            session_id=body["session_id"],
            channel_tenant_id=4,
            channel_id=2,
        )
        joined = client.post(f"/sessions/{body['session_id']}/join", headers=_bearer(token))
        ended = client.post(f"/sessions/{body['session_id']}/end", headers=_bearer(token))
        again = client.post(f"/sessions/{body['session_id']}/end", headers=_bearer(token))
        rejected = client.post(f"/sessions/{body['session_id']}/join", headers=_bearer(token))

    assert joined.status_code == 200
    assert len(store.sessions) == 1
    assert store.sessions[(4, body["session_id"])].description == "planning"
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    assert again.status_code == 409
    assert rejected.status_code == 409
    assert store.speakers == []


def test_other_tenant_session_is_not_found() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    store.allow(5, 7)
    token = issue_client_token(SECRET, 4, datetime.now(timezone.utc), timedelta(minutes=5))
    other = issue_client_token(SECRET, 5, datetime.now(timezone.utc), timedelta(minutes=5))

    with TestClient(create_app(hot_settings(), store)) as client:
        created = client.post("/sessions", headers=_bearer(token), json={"channel_id": 2})
        session_id = created.json()["session_id"]
        response = client.post(f"/sessions/{session_id}/join", headers=_bearer(other))

    assert response.status_code == 404


def test_channel_outside_the_tenant_is_forbidden() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    store.channels[8] = (9, 8)
    token = issue_client_token(SECRET, 4, datetime.now(timezone.utc), timedelta(minutes=5))

    with TestClient(create_app(hot_settings(), store)) as client:
        missing = client.post("/sessions", headers=_bearer(token), json={"channel_id": 99})
        forbidden = client.post("/sessions", headers=_bearer(token), json={"channel_id": 8})

    assert missing.status_code == 404
    assert forbidden.status_code == 403


def test_generic_channel_can_open_a_session() -> None:
    store = MemoryControlStore()
    store.allow(4, 3, channel_tenant_id=1)
    token = issue_client_token(SECRET, 4, datetime.now(timezone.utc), timedelta(minutes=5))

    with TestClient(create_app(hot_settings(), store)) as client:
        response = client.post("/sessions", headers=_bearer(token), json={"channel_id": 3})

    assert response.status_code == 201
    session = store.sessions[(4, response.json()["session_id"])]
    assert session.channel_tenant_id == 1


def test_health_does_not_open_a_database_connection() -> None:
    app = create_app(hot_settings())

    with TestClient(app) as client:
        response = client.get("/health")
        engine = app.state.engine

    assert response.status_code == 200
    assert engine is None


def test_missing_channel_id_is_rejected() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token = issue_client_token(SECRET, 4, datetime.now(timezone.utc), timedelta(minutes=5))

    with TestClient(create_app(hot_settings(), store)) as client:
        response = client.post("/sessions", headers=_bearer(token), json={})

    assert response.status_code == 422
