"""Signed client and signaling credentials for the hot-server control API.

Both tokens are HS256 JWTs signed with the host signaling secret. The client
token authorizes create, join, and end for one tenant. The signaling token
authorizes one connection to one session. Neither token is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_TOKEN_TYPE = "JWT"
_ALGORITHM = "HS256"


class InvalidCredential(ValueError):
    """Raised when a credential is missing, forged, expired, or mismatched."""


@dataclass(frozen=True)
class ClientPrincipal:
    """Tenant authorized by a verified client credential."""

    tenant_id: int


@dataclass(frozen=True)
class SignalingGrant:
    """Session binding carried by a verified signaling credential."""

    tenant_id: int
    session_id: int
    channel_tenant_id: int
    channel_id: int
    expires_at: datetime


def issue_client_token(secret: str, tenant_id: int, now: datetime, ttl: timedelta) -> str:
    """Sign a client credential for one tenant."""
    return _sign(
        secret,
        {
            "token_use": "client",
            "tenant_id": tenant_id,
            "exp": int((now + ttl).timestamp()),
        },
    )


def issue_signaling_token(
    secret: str,
    *,
    tenant_id: int,
    session_id: int,
    channel_tenant_id: int,
    channel_id: int,
    now: datetime,
    ttl: timedelta,
) -> tuple[str, datetime]:
    """Sign a signaling credential and return it with its expiry."""
    expires_at = datetime.fromtimestamp(int((now + ttl).timestamp()), timezone.utc)
    token = _sign(
        secret,
        {
            "token_use": "signaling",
            "tenant_id": tenant_id,
            "session_id": session_id,
            "channel_tenant_id": channel_tenant_id,
            "channel_id": channel_id,
            "exp": int(expires_at.timestamp()),
        },
    )
    return token, expires_at


def verify_client_token(token: str, secret: str, now: datetime) -> ClientPrincipal:
    """Return the tenant in a client credential."""
    payload = _verified_payload(token, secret, now)
    if payload.get("token_use") != "client":
        raise InvalidCredential("credential is not a client credential")
    return ClientPrincipal(tenant_id=_positive_int(payload, "tenant_id"))


def signaling_grant(token: str, secret: str, now: datetime) -> SignalingGrant:
    """Return the session binding in a signaling credential."""
    payload = _verified_payload(token, secret, now)
    if payload.get("token_use") != "signaling":
        raise InvalidCredential("credential is not a signaling credential")
    expires_at = datetime.fromtimestamp(_expiry(payload), timezone.utc)
    return SignalingGrant(
        tenant_id=_positive_int(payload, "tenant_id"),
        session_id=_positive_int(payload, "session_id"),
        channel_tenant_id=_positive_int(payload, "channel_tenant_id"),
        channel_id=_positive_int(payload, "channel_id"),
        expires_at=expires_at,
    )


def authorize_signaling(
    grant: SignalingGrant,
    *,
    tenant_id: int,
    session_id: int,
    channel_tenant_id: int,
    channel_id: int,
) -> None:
    """Reject a signaling grant that names a different session, tenant, or channel."""
    if grant.tenant_id != tenant_id:
        raise InvalidCredential("credential tenant does not match")
    if grant.session_id != session_id:
        raise InvalidCredential("credential session does not match")
    if grant.channel_tenant_id != channel_tenant_id or grant.channel_id != channel_id:
        raise InvalidCredential("credential channel does not match")


def _sign(secret: str, payload: dict[str, object]) -> str:
    header = _encode({"alg": _ALGORITHM, "typ": _TOKEN_TYPE})
    body = _encode(payload)
    signature = _signature(secret, f"{header}.{body}")
    return f"{header}.{body}.{signature}"


def _verified_payload(token: str, secret: str, now: datetime) -> dict[str, object]:
    header_segment, payload_segment, signature_segment = _segments(token)
    header = _decode_object(header_segment)
    if header.get("alg") != _ALGORITHM:
        raise InvalidCredential("credential algorithm is not HS256")
    signing_input = f"{header_segment}.{payload_segment}"
    expected = _raw_signature(secret, signing_input)
    supplied = _decode_bytes(signature_segment) if signature_segment else b""
    if len(supplied) != len(expected) or not hmac.compare_digest(supplied, expected):
        raise InvalidCredential("credential signature is invalid")
    payload = _decode_object(payload_segment)
    _reject_expired(payload, now)
    return payload


def _segments(token: str) -> tuple[str, str, str]:
    parts = token.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise InvalidCredential("credential is malformed")
    return parts[0], parts[1], parts[2]


def _encode(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_object(segment: str) -> dict[str, object]:
    try:
        value = json.loads(_decode_bytes(segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidCredential("credential is malformed") from error
    if not isinstance(value, dict):
        raise InvalidCredential("credential is malformed")
    return value


def _decode_bytes(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except ValueError as error:
        raise InvalidCredential("credential is malformed") from error


def _signature(secret: str, signing_input: str) -> str:
    raw = _raw_signature(secret, signing_input)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _raw_signature(secret: str, signing_input: str) -> bytes:
    return hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()


def _reject_expired(payload: dict[str, object], now: datetime) -> None:
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        raise InvalidCredential("credential expiry is missing")
    if exp <= int(now.timestamp()):
        raise InvalidCredential("credential is expired")


def _expiry(payload: dict[str, object]) -> int:
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        raise InvalidCredential("credential expiry is missing")
    return exp


def _positive_int(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidCredential(f"credential {name} is invalid")
    return value
