"""Short-lived signaling credentials.

A signaling credential authorizes one connection to one session. It is signed
with the signaling secret, not the control secret, and it is not a control
credential. The credential is not stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NoReturn

from ceho.logging import get_logger

logger = get_logger(__name__)

_USE = "signaling"


class SignalingCredentialError(ValueError):
    """A signaling credential was missing, forged, expired, or mismatched."""


@dataclass(frozen=True)
class SignalingGrant:
    """Session binding carried by a verified signaling credential."""

    tenant_id: int
    session_id: int
    channel_tenant_id: int
    channel_id: int
    expires_at: datetime


def issue_signaling_credential(
    secret: str,
    *,
    tenant_id: int,
    session_id: int,
    channel_tenant_id: int,
    channel_id: int,
    now: datetime,
    ttl_seconds: int,
) -> tuple[str, datetime]:
    """Sign a signaling credential and return it with its expiry."""
    moment = _aware(now)
    expires_at = datetime.fromtimestamp(
        int((moment + timedelta(seconds=ttl_seconds)).timestamp()),
        timezone.utc,
    )
    body = _body(
        tenant_id=tenant_id,
        session_id=session_id,
        channel_tenant_id=channel_tenant_id,
        channel_id=channel_id,
        expires_at=expires_at,
    )
    signature = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return f"{_encode(body)}.{_encode(signature)}", expires_at


def read_signaling_credential(
    credential: str,
    secret: str,
    *,
    now: datetime,
) -> SignalingGrant:
    """Return the session binding in a credential that has not expired."""
    moment = _aware(now)
    payload = _payload(credential, secret)
    expires_at = datetime.fromtimestamp(payload["exp"], timezone.utc)
    if moment >= expires_at:
        _reject("expired")
    return SignalingGrant(
        tenant_id=payload["tenant_id"],
        session_id=payload["session_id"],
        channel_tenant_id=payload["channel_tenant_id"],
        channel_id=payload["channel_id"],
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
    """Reject a grant that names a different session, tenant, or channel."""
    if grant.tenant_id != tenant_id:
        _reject("tenant")
    if grant.session_id != session_id:
        _reject("session")
    if grant.channel_tenant_id != channel_tenant_id or grant.channel_id != channel_id:
        _reject("channel")


def _body(
    *,
    tenant_id: int,
    session_id: int,
    channel_tenant_id: int,
    channel_id: int,
    expires_at: datetime,
) -> bytes:
    return json.dumps(
        {
            "channel_id": channel_id,
            "channel_tenant_id": channel_tenant_id,
            "exp": int(expires_at.timestamp()),
            "session_id": session_id,
            "tenant_id": tenant_id,
            "use": _USE,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _payload(credential: str, secret: str) -> Mapping[str, int]:
    body_text, signature_text = _parts(credential)
    try:
        body = _decode(body_text)
        signature = _decode(signature_text)
    except ValueError:
        _reject("invalid")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        _reject("invalid")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        _reject("invalid")
    if not isinstance(parsed, dict) or parsed.get("use") != _USE:
        _reject("invalid")
    exp = parsed.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        _reject("invalid")
    return {
        "exp": exp,
        "tenant_id": _positive_id(parsed.get("tenant_id")),
        "session_id": _positive_id(parsed.get("session_id")),
        "channel_tenant_id": _positive_id(parsed.get("channel_tenant_id")),
        "channel_id": _positive_id(parsed.get("channel_id")),
    }


def _parts(credential: str) -> tuple[str, str]:
    parts = credential.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        _reject("invalid")
    return parts[0], parts[1]


def _positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _reject("invalid")
    return value


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise SignalingCredentialError("signaling credential rejected")
    return now


def _reject(reason: str) -> NoReturn:
    logger.debug("signaling credential rejected", reason=reason)
    logger.error("signaling credential rejected", reason=reason)
    if reason == "expired":
        raise SignalingCredentialError("signaling credential expired")
    if reason == "tenant":
        raise SignalingCredentialError("signaling credential tenant does not match")
    if reason == "session":
        raise SignalingCredentialError("signaling credential session does not match")
    if reason == "channel":
        raise SignalingCredentialError("signaling credential channel does not match")
    raise SignalingCredentialError("signaling credential rejected")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
