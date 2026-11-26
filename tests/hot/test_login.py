"""Password login and the control credential it returns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from ceho.hot.auth import (
    AuthenticationError,
    LoginAccount,
    authenticate,
    read_control_credential,
)
from ceho.hot.config import HotServerSettings
from ceho.hot.server import create_app

_HASHER = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
_NOW = datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc)
_SECRET = "control-secret"
_PASSWORD = "correct-horse"


class _Row:
    def __init__(self, email: str, account: LoginAccount) -> None:
        self.email = email
        self.account = account


class _Accounts:
    """In-memory accounts. The login path does not open a database."""

    def __init__(self, rows: tuple[_Row, ...]) -> None:
        self._rows = rows

    def find_by_email(self, email: str) -> tuple[LoginAccount, ...]:
        return tuple(row.account for row in self._rows if row.email == email)


def _row(
    *,
    user_id: int = 4,
    tenant_id: int = 9,
    email: str = "ada@example.com",
    password: str | None = _PASSWORD,
) -> _Row:
    return _Row(
        email,
        LoginAccount(
            user_id=user_id,
            tenant_id=tenant_id,
            password_hash=None if password is None else _HASHER.hash(password),
        ),
    )


def _settings() -> HotServerSettings:
    return HotServerSettings(
        database_url="postgresql://ceho:ceho@localhost/ceho",
        media_storage_uri="s3://ceho-media",
        s3_endpoint_url="http://minio:9000",
        s3_access_key_id="ceho",
        s3_secret_access_key="ceho-dev-only",
        s3_region="us-east-1",
        signaling_secret="signaling-secret",
        control_secret=_SECRET,
        event_publisher_url="pgmq://ceho",
        host="127.0.0.1",
        port=8000,
        control_credential_ttl_seconds=900,
    )


def _client(rows: tuple[_Row, ...]) -> TestClient:
    app = create_app(_settings(), login_accounts=_Accounts(rows), now=lambda: _NOW)
    return TestClient(app)


def test_login_returns_a_credential_for_the_account_tenant() -> None:
    with _client((_row(user_id=4, tenant_id=9),)) as client:
        response = client.post(
            "/login",
            json={"email": "  Ada@Example.com ", "password": _PASSWORD, "tenant_id": 1},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == 4
    assert body["tenant_id"] == 9
    principal = read_control_credential(body["credential"], _SECRET, now=_NOW)
    assert principal.user_id == 4
    assert principal.tenant_id == 9
    assert principal.expires_at == _NOW + timedelta(seconds=900)


@pytest.mark.parametrize(
    "accounts,payload",
    [
        ((), {"email": "ada@example.com", "password": _PASSWORD}),
        ((_row(),), {"email": "ada@example.com", "password": "wrong-password"}),
        ((_row(password=None),), {"email": "ada@example.com", "password": _PASSWORD}),
        ((_row(),), {"email": "  ", "password": _PASSWORD}),
        ((_row(),), {"email": "ada@example.com", "password": ""}),
    ],
)
def test_rejected_login_is_the_same_response(
    accounts: tuple[_Row, ...],
    payload: dict[str, str],
) -> None:
    with _client(accounts) as client:
        response = client.post("/login", json=payload)

    assert response.status_code == 401
    assert response.json() == {"detail": "login rejected"}
    assert _PASSWORD not in response.text
    assert "$argon2id$" not in response.text


def test_two_matching_passwords_are_rejected() -> None:
    accounts = (
        _row(user_id=1, tenant_id=2, password=_PASSWORD),
        _row(user_id=3, tenant_id=5, password=_PASSWORD),
    )
    with _client(accounts) as client:
        response = client.post(
            "/login",
            json={"email": "ada@example.com", "password": _PASSWORD},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "login rejected"}


def test_one_matching_password_selects_that_account() -> None:
    accounts = (
        _row(user_id=1, tenant_id=2, password=None),
        _row(user_id=3, tenant_id=5, password=_PASSWORD),
    )
    with _client(accounts) as client:
        response = client.post(
            "/login",
            json={"email": "ada@example.com", "password": _PASSWORD},
        )

    assert response.status_code == 200
    assert response.json()["user_id"] == 3
    assert response.json()["tenant_id"] == 5


def test_expired_control_credential_is_rejected() -> None:
    issued = authenticate(
        _Accounts((_row(),)),
        "ada@example.com",
        _PASSWORD,
        secret=_SECRET,
        now=_NOW,
        ttl_seconds=900,
    )

    with pytest.raises(AuthenticationError, match="control credential expired"):
        read_control_credential(issued.credential, _SECRET, now=issued.expires_at)
    principal = read_control_credential(
        issued.credential,
        _SECRET,
        now=issued.expires_at - timedelta(seconds=1),
    )
    assert principal.tenant_id == 9


def test_other_secret_and_tampered_credential_are_rejected() -> None:
    issued = authenticate(
        _Accounts((_row(),)),
        "ada@example.com",
        _PASSWORD,
        secret=_SECRET,
        now=_NOW,
        ttl_seconds=900,
    )

    with pytest.raises(AuthenticationError, match="control credential rejected"):
        read_control_credential(issued.credential, "signaling-secret", now=_NOW)
    with pytest.raises(AuthenticationError, match="control credential rejected"):
        read_control_credential(issued.credential + "x", _SECRET, now=_NOW)


def test_directory_failure_rejects_without_echoing_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("ERROR", logger="ceho.hot.server")

    class _Broken:
        def find_by_email(self, email: str) -> tuple[LoginAccount, ...]:
            raise RuntimeError(_PASSWORD)

    app = create_app(_settings(), login_accounts=_Broken(), now=lambda: _NOW)
    with TestClient(app) as client:
        response = client.post(
            "/login",
            json={"email": "ada@example.com", "password": _PASSWORD},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "login is unavailable"}
    assert _PASSWORD not in response.text
    assert any(record.msg == "login failed" for record in caplog.records)


def test_login_without_a_directory_or_database_is_unavailable() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.post(
            "/login",
            json={"email": "ada@example.com", "password": _PASSWORD},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "login is unavailable"}


def test_login_logs_do_not_carry_the_password_or_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="ceho.hot.auth")
    with _client((_row(),)) as client:
        success = client.post(
            "/login",
            json={"email": "ada@example.com", "password": _PASSWORD},
        )
        failure = client.post(
            "/login",
            json={"email": "ada@example.com", "password": "wrong-password"},
        )

    assert success.status_code == 200
    assert failure.status_code == 401
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
    assert _PASSWORD not in rendered
    assert "wrong-password" not in rendered
    assert success.json()["credential"] not in rendered
    assert "$argon2id$" not in rendered
    assert any(record.msg == "login succeeded" for record in caplog.records)
    assert any(record.msg == "login rejected" for record in caplog.records)
