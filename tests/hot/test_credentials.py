"""Tests for client and signaling credentials."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from ceho.hot.credentials import (
    InvalidCredential,
    authorize_signaling,
    issue_client_token,
    issue_signaling_token,
    signaling_grant,
    verify_client_token,
)

SECRET = "test-secret"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(seconds=60)


def _payload(token: str) -> dict[str, object]:
    segment = token.split(".")[1]
    padded = segment + "=" * (-len(segment) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded))
    assert isinstance(value, dict)
    return value


def test_client_token_resolves_its_tenant() -> None:
    token = issue_client_token(SECRET, 4, NOW, TTL)

    principal = verify_client_token(token, SECRET, NOW)

    assert principal.tenant_id == 4
    assert set(_payload(token)) == {"token_use", "tenant_id", "exp"}


def test_client_token_rejects_a_tampered_tenant() -> None:
    token = issue_client_token(SECRET, 4, NOW, TTL)
    header, payload, signature = token.split(".")
    claims = _payload(token)
    claims["tenant_id"] = 9
    forged_payload = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        .rstrip(b"=")
        .decode("ascii")
    )

    with pytest.raises(InvalidCredential, match="signature"):
        verify_client_token(f"{header}.{forged_payload}.{signature}", SECRET, NOW)


def test_client_token_rejects_the_wrong_secret() -> None:
    token = issue_client_token(SECRET, 4, NOW, TTL)

    with pytest.raises(InvalidCredential, match="signature"):
        verify_client_token(token, "other-secret", NOW)


def test_client_token_rejects_an_expired_credential() -> None:
    token = issue_client_token(SECRET, 4, NOW, TTL)

    with pytest.raises(InvalidCredential, match="expired"):
        verify_client_token(token, SECRET, NOW + TTL)


def test_client_token_rejects_a_malformed_credential() -> None:
    with pytest.raises(InvalidCredential, match="malformed"):
        verify_client_token("not-a-token", SECRET, NOW)


def test_client_token_rejects_an_unsigned_algorithm() -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    body = (
        base64.urlsafe_b64encode(b'{"exp":9999999999,"tenant_id":4,"token_use":"client"}')
        .rstrip(b"=")
        .decode("ascii")
    )

    with pytest.raises(InvalidCredential, match="HS256"):
        verify_client_token(f"{header}.{body}.", SECRET, NOW)


def test_signaling_token_is_rejected_as_a_client_credential() -> None:
    token, _expires_at = issue_signaling_token(
        SECRET,
        tenant_id=4,
        session_id=10,
        channel_tenant_id=4,
        channel_id=2,
        now=NOW,
        ttl=TTL,
    )

    with pytest.raises(InvalidCredential, match="not a client credential"):
        verify_client_token(token, SECRET, NOW)


def test_client_token_is_rejected_as_a_signaling_credential() -> None:
    token = issue_client_token(SECRET, 4, NOW, TTL)

    with pytest.raises(InvalidCredential, match="not a signaling credential"):
        signaling_grant(token, SECRET, NOW)


def test_signaling_token_names_the_connection_only() -> None:
    token, expires_at = issue_signaling_token(
        SECRET,
        tenant_id=4,
        session_id=10,
        channel_tenant_id=1,
        channel_id=2,
        now=NOW,
        ttl=TTL,
    )

    grant = signaling_grant(token, SECRET, NOW)

    assert grant.tenant_id == 4
    assert grant.session_id == 10
    assert grant.channel_tenant_id == 1
    assert grant.channel_id == 2
    assert grant.expires_at == expires_at
    assert set(_payload(token)) == {
        "token_use",
        "tenant_id",
        "session_id",
        "channel_tenant_id",
        "channel_id",
        "exp",
    }


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
    token, _expires_at = issue_signaling_token(
        SECRET,
        tenant_id=4,
        session_id=10,
        channel_tenant_id=4,
        channel_id=2,
        now=NOW,
        ttl=TTL,
    )
    grant = signaling_grant(token, SECRET, NOW)
    binding = {
        "tenant_id": 4,
        "session_id": 10,
        "channel_tenant_id": 4,
        "channel_id": 2,
    }
    binding[field] = value

    with pytest.raises(InvalidCredential, match=message):
        authorize_signaling(
            grant,
            tenant_id=binding["tenant_id"],
            session_id=binding["session_id"],
            channel_tenant_id=binding["channel_tenant_id"],
            channel_id=binding["channel_id"],
        )


def test_expired_signaling_credential_is_rejected() -> None:
    token, _expires_at = issue_signaling_token(
        SECRET,
        tenant_id=4,
        session_id=10,
        channel_tenant_id=4,
        channel_id=2,
        now=NOW,
        ttl=TTL,
    )

    with pytest.raises(InvalidCredential, match="expired"):
        signaling_grant(token, SECRET, NOW + TTL)
