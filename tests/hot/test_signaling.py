"""Tests for signaling credentials."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ceho.hot.auth import AuthenticationError, read_control_credential
from ceho.hot.signaling import (
    SignalingCredentialError,
    authorize_signaling,
    issue_signaling_credential,
    read_signaling_credential,
)

SECRET = "signaling-secret"
NOW = datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc)
TTL = 60


def _issue(**overrides: int) -> str:
    values = {
        "tenant_id": 4,
        "session_id": 10,
        "channel_tenant_id": 4,
        "channel_id": 2,
    }
    values.update(overrides)
    credential, _expires_at = issue_signaling_credential(
        SECRET,
        tenant_id=values["tenant_id"],
        session_id=values["session_id"],
        channel_tenant_id=values["channel_tenant_id"],
        channel_id=values["channel_id"],
        now=NOW,
        ttl_seconds=TTL,
    )
    return credential


def test_signaling_credential_names_the_connection_only() -> None:
    credential, expires_at = issue_signaling_credential(
        SECRET,
        tenant_id=4,
        session_id=10,
        channel_tenant_id=1,
        channel_id=2,
        now=NOW,
        ttl_seconds=TTL,
    )

    grant = read_signaling_credential(credential, SECRET, now=NOW)

    assert grant.tenant_id == 4
    assert grant.session_id == 10
    assert grant.channel_tenant_id == 1
    assert grant.channel_id == 2
    assert grant.expires_at == expires_at
    authorize_signaling(
        grant,
        tenant_id=4,
        session_id=10,
        channel_tenant_id=1,
        channel_id=2,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_id", 99, "session"),
        ("tenant_id", 8, "tenant"),
        ("channel_id", 3, "channel"),
        ("channel_tenant_id", 8, "channel"),
    ],
)
def test_signaling_credential_rejects_a_different_binding(
    field: str,
    value: int,
    message: str,
) -> None:
    grant = read_signaling_credential(_issue(), SECRET, now=NOW)
    binding = {
        "tenant_id": 4,
        "session_id": 10,
        "channel_tenant_id": 4,
        "channel_id": 2,
    }
    binding[field] = value

    with pytest.raises(SignalingCredentialError, match=message):
        authorize_signaling(
            grant,
            tenant_id=binding["tenant_id"],
            session_id=binding["session_id"],
            channel_tenant_id=binding["channel_tenant_id"],
            channel_id=binding["channel_id"],
        )


def test_expired_signaling_credential_is_rejected() -> None:
    credential, expires_at = issue_signaling_credential(
        SECRET,
        tenant_id=4,
        session_id=10,
        channel_tenant_id=4,
        channel_id=2,
        now=NOW,
        ttl_seconds=TTL,
    )

    with pytest.raises(SignalingCredentialError, match="expired"):
        read_signaling_credential(credential, SECRET, now=expires_at)
    grant = read_signaling_credential(
        credential,
        SECRET,
        now=expires_at - timedelta(seconds=1),
    )
    assert grant.session_id == 10


def test_tampered_signaling_credential_is_rejected() -> None:
    credential = _issue()

    with pytest.raises(SignalingCredentialError, match="rejected"):
        read_signaling_credential(credential + "x", SECRET, now=NOW)
    with pytest.raises(SignalingCredentialError, match="rejected"):
        read_signaling_credential(credential, "other-secret", now=NOW)


def test_malformed_signaling_credential_is_rejected() -> None:
    with pytest.raises(SignalingCredentialError, match="rejected"):
        read_signaling_credential("not-a-credential", SECRET, now=NOW)


def test_control_and_signaling_credentials_are_not_interchangeable() -> None:
    from argon2 import PasswordHasher

    from ceho.hot.auth import LoginAccount, authenticate

    password = "correct-horse"
    hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)

    class _Accounts:
        def find_by_email(self, email: str) -> tuple[LoginAccount, ...]:
            return (LoginAccount(user_id=4, tenant_id=4, password_hash=hasher.hash(password)),)

    control = authenticate(
        _Accounts(),
        "ada@example.com",
        password,
        secret="control-secret",
        now=NOW,
        ttl_seconds=900,
    ).credential
    signaling = _issue()

    with pytest.raises(SignalingCredentialError, match="rejected"):
        read_signaling_credential(control, SECRET, now=NOW)
    with pytest.raises(AuthenticationError, match="control credential rejected"):
        read_control_credential(signaling, "control-secret", now=NOW)
