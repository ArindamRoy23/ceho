"""HTTP contract for session create, join, and end."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from ceho.hot.auth import LoginAccount, authenticate
from ceho.hot.server import create_app
from ceho.hot.signaling import (
    SignalingCredentialError,
    authorize_signaling,
    issue_signaling_credential,
    read_signaling_credential,
)
from ceho.hot.turn import IceRelay
from tests.hot.conftest import MemoryControlStore, hot_settings

NOW = datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc)
CONTROL_SECRET = "control-secret"
SIGNALING_SECRET = "signaling-secret"
_PASSWORD = "correct-horse"
_HASHER = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


class _Accounts:
    def __init__(self, account: LoginAccount) -> None:
        self._account = account

    def find_by_email(self, email: str) -> tuple[LoginAccount, ...]:
        if email == "ada@example.com":
            return (self._account,)
        return ()


def _credential(
    *,
    tenant_id: int = 4,
    user_id: int = 7,
    now: datetime = NOW,
    ttl_seconds: int = 900,
) -> str:
    account = LoginAccount(
        user_id=user_id,
        tenant_id=tenant_id,
        password_hash=_HASHER.hash(_PASSWORD),
    )
    return authenticate(
        _Accounts(account),
        "ada@example.com",
        _PASSWORD,
        secret=CONTROL_SECRET,
        now=now,
        ttl_seconds=ttl_seconds,
    ).credential


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Turn:
    def mint(self, *, ttl_seconds: int) -> IceRelay:
        return IceRelay(
            stun_url="stun:stun.cloudflare.com:3478",
            turn_url="turns:turn.cloudflare.com:443?transport=tcp",
            username="turn-user",
            password="turn-pass",
        )


def _app(store: MemoryControlStore) -> TestClient:
    app = create_app(hot_settings(), control_store=store, now=lambda: NOW, turn=_Turn())
    return TestClient(app)


def test_missing_control_credential_is_rejected() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)

    with _app(store) as client:
        response = client.post("/sessions", json={"channel_id": 2})

    assert response.status_code == 401
    assert response.json()["detail"] == "control credential is missing"
    assert store.sessions == {}


def test_signaling_credential_is_rejected_on_create() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token, _expires_at = issue_signaling_credential(
        SIGNALING_SECRET,
        tenant_id=4,
        session_id=1,
        channel_tenant_id=4,
        channel_id=2,
        now=NOW,
        ttl_seconds=60,
    )

    with _app(store) as client:
        response = client.post("/sessions", headers=_bearer(token), json={"channel_id": 2})

    assert response.status_code == 401
    assert store.sessions == {}


def test_expired_control_credential_is_rejected() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token = _credential(now=NOW - timedelta(hours=1), ttl_seconds=60)

    with _app(store) as client:
        response = client.post("/sessions", headers=_bearer(token), json={"channel_id": 2})

    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_create_join_and_end_stay_inside_the_authenticated_tenant() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token = _credential()

    with _app(store) as client:
        created = client.post(
            "/sessions",
            headers=_bearer(token),
            json={"channel_id": 2, "tenant_id": 9, "description": "planning"},
        )
        assert created.status_code == 201
        body = created.json()
        grant = read_signaling_credential(body["signaling_credential"], SIGNALING_SECRET, now=NOW)
        authorize_signaling(
            grant,
            tenant_id=4,
            session_id=body["session_id"],
            channel_tenant_id=4,
            channel_id=2,
        )
        with pytest.raises(SignalingCredentialError, match="session"):
            authorize_signaling(
                grant,
                tenant_id=4,
                session_id=body["session_id"] + 1,
                channel_tenant_id=4,
                channel_id=2,
            )
        joined = client.post(f"/sessions/{body['session_id']}/join", headers=_bearer(token))
        again = client.post(f"/sessions/{body['session_id']}/join", headers=_bearer(token))
        ended = client.post(f"/sessions/{body['session_id']}/end", headers=_bearer(token))
        second_end = client.post(f"/sessions/{body['session_id']}/end", headers=_bearer(token))
        rejected = client.post(f"/sessions/{body['session_id']}/join", headers=_bearer(token))

    assert joined.status_code == 200
    assert again.status_code == 200
    assert len(store.sessions) == 1
    assert store.sessions[(4, body["session_id"])].description == "planning"
    assert [event.event_type for event in store.outbox] == ["session.created", "session.ended"]
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"
    assert second_end.status_code == 409
    assert rejected.status_code == 409


def test_other_tenant_session_is_not_found() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    store.allow(5, 7)
    token = _credential()
    other = _credential(tenant_id=5, user_id=8)

    with _app(store) as client:
        created = client.post("/sessions", headers=_bearer(token), json={"channel_id": 2})
        session_id = created.json()["session_id"]
        response = client.post(f"/sessions/{session_id}/join", headers=_bearer(other))

    assert created.status_code == 201
    assert response.status_code == 404


def test_channel_outside_the_tenant_is_forbidden() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    store.channels[8] = (9, 8)
    token = _credential()

    with _app(store) as client:
        missing = client.post("/sessions", headers=_bearer(token), json={"channel_id": 99})
        forbidden = client.post("/sessions", headers=_bearer(token), json={"channel_id": 8})

    assert missing.status_code == 404
    assert forbidden.status_code == 403
    assert store.sessions == {}


def test_generic_channel_can_open_a_session() -> None:
    store = MemoryControlStore()
    store.allow(4, 3, channel_tenant_id=1)
    token = _credential()

    with _app(store) as client:
        response = client.post("/sessions", headers=_bearer(token), json={"channel_id": 3})

    assert response.status_code == 201
    session = store.sessions[(4, response.json()["session_id"])]
    assert session.channel_tenant_id == 1


def test_missing_channel_id_is_rejected() -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token = _credential()

    with _app(store) as client:
        response = client.post("/sessions", headers=_bearer(token), json={})

    assert response.status_code == 422
    assert store.sessions == {}


def test_session_control_without_a_store_or_database_is_unavailable() -> None:
    token = _credential()

    with TestClient(create_app(hot_settings(), now=lambda: NOW)) as client:
        response = client.post("/sessions", headers=_bearer(token), json={"channel_id": 2})

    assert response.status_code == 503
    assert response.json()["detail"] == "session control is unavailable"


def test_session_logs_do_not_carry_the_credential(caplog: pytest.LogCaptureFixture) -> None:
    store = MemoryControlStore()
    store.allow(4, 2)
    token = _credential()
    caplog.set_level("DEBUG", logger="ceho.hot.session")

    with _app(store) as client:
        created = client.post(
            "/sessions",
            headers=_bearer(token),
            json={"channel_id": 2},
        )

    assert created.status_code == 201
    credential = created.json()["signaling_credential"]
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
    assert token not in rendered
    assert any(record.msg == "session created" for record in caplog.records)
