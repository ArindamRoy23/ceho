"""Password login and the short-lived control credential it returns.

The credential names the user and tenant from the account row. It is signed
with the control secret, not the signaling secret, and it is not a join
credential. Callers do not send a tenant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NoReturn, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select
from sqlalchemy.engine import Connection

from ceho.logging import get_logger
from ceho.models import User

logger = get_logger(__name__)

_HASHER = PasswordHasher()
_BURN = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)
_DUMMY_HASH = _BURN.hash("ceho-control-login")
_USE = "control"


class AuthenticationError(Exception):
    """A password or control credential was rejected."""


class LoginUnavailable(Exception):
    """Password login cannot reach an account directory."""


@dataclass(frozen=True)
class LoginAccount:
    """One platform account that may prove itself with a password."""

    user_id: int
    tenant_id: int
    password_hash: str | None


@dataclass(frozen=True)
class ControlCredential:
    """A signed control credential and the account it names."""

    credential: str
    expires_at: datetime
    user_id: int
    tenant_id: int


@dataclass(frozen=True)
class ControlPrincipal:
    """The account named by a control credential that is still valid."""

    user_id: int
    tenant_id: int
    expires_at: datetime


class LoginAccounts(Protocol):
    """Accounts addressed by the normalized email stored on the user row."""

    def find_by_email(self, email: str) -> tuple[LoginAccount, ...]:
        """Return every account whose stored email equals ``email``."""


class DatabaseLoginAccounts:
    """Read platform accounts from ``ceho_core.users``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def find_by_email(self, email: str) -> tuple[LoginAccount, ...]:
        table = User.__table__
        rows = self._connection.execute(
            select(table.c.user_id, table.c.tenant_id, table.c.password_hash).where(
                table.c.email == email
            )
        )
        return tuple(
            LoginAccount(
                user_id=row.user_id,
                tenant_id=row.tenant_id,
                password_hash=row.password_hash,
            )
            for row in rows
        )


def authenticate(
    accounts: LoginAccounts,
    email: str,
    password: str,
    *,
    secret: str,
    now: datetime,
    ttl_seconds: int,
) -> ControlCredential:
    """Prove one account and return its control credential.

    An unknown email, a wrong password, a missing hash, a blank login, and
    more than one matching password all raise the same error. The tenant is
    taken from the single matching row.
    """
    moment = _aware(now, "login rejected")
    normalized = email.strip().lower()
    if not normalized or password == "":
        _burn(password)
        _reject_login("blank")
    matches = accounts.find_by_email(normalized)
    verified = _verified(matches, password)
    if len(verified) != 1:
        _reject_matches(matches, verified)
    account = verified[0]
    expires_at = datetime.fromtimestamp(
        int((moment + timedelta(seconds=ttl_seconds)).timestamp()),
        timezone.utc,
    )
    credential = _sign(account.user_id, account.tenant_id, secret, expires_at)
    logger.info(
        "login succeeded",
        user_id=account.user_id,
        tenant_id=account.tenant_id,
        expires_at=_stamp(expires_at),
    )
    return ControlCredential(
        credential=credential,
        expires_at=expires_at,
        user_id=account.user_id,
        tenant_id=account.tenant_id,
    )


def read_control_credential(
    credential: str,
    secret: str,
    *,
    now: datetime,
) -> ControlPrincipal:
    """Return the account named by a credential that has not expired."""
    moment = _aware(now, "control credential rejected")
    payload = _payload(credential, secret)
    expires_at = datetime.fromtimestamp(payload["exp"], timezone.utc)
    if moment >= expires_at:
        _reject_credential("expired")
    return ControlPrincipal(
        user_id=payload["user_id"],
        tenant_id=payload["tenant_id"],
        expires_at=expires_at,
    )


def _verified(matches: tuple[LoginAccount, ...], password: str) -> list[LoginAccount]:
    verified: list[LoginAccount] = []
    saw_hash = False
    for account in matches:
        if account.password_hash is None:
            continue
        saw_hash = True
        try:
            _HASHER.verify(account.password_hash, password)
        except VerificationError:
            continue
        else:
            verified.append(account)
    if not saw_hash:
        _burn(password)
    return verified


def _reject_matches(matches: tuple[LoginAccount, ...], verified: list[LoginAccount]) -> NoReturn:
    if len(verified) > 1:
        _reject_login("ambiguous")
    if not matches:
        _reject_login("unknown")
    hashed = [account for account in matches if account.password_hash is not None]
    if not hashed and len(matches) == 1:
        account = matches[0]
        _reject_login("no_password", user_id=account.user_id, tenant_id=account.tenant_id)
    if len(hashed) == 1:
        account = hashed[0]
        _reject_login("password", user_id=account.user_id, tenant_id=account.tenant_id)
    _reject_login("password" if hashed else "no_password")


def _sign(user_id: int, tenant_id: int, secret: str, expires_at: datetime) -> str:
    body = json.dumps(
        {
            "exp": int(expires_at.timestamp()),
            "tenant_id": tenant_id,
            "use": _USE,
            "user_id": user_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return f"{_encode(body)}.{_encode(signature)}"


def _payload(credential: str, secret: str) -> Mapping[str, int]:
    body_text, signature_text = _parts(credential)
    try:
        body = _decode(body_text)
        signature = _decode(signature_text)
    except ValueError:
        _reject_credential("invalid")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        _reject_credential("invalid")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        _reject_credential("invalid")
    if not isinstance(parsed, dict) or parsed.get("use") != _USE:
        _reject_credential("invalid")
    exp = parsed.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int):
        _reject_credential("invalid")
    return {
        "exp": exp,
        "user_id": _account_id(parsed.get("user_id")),
        "tenant_id": _account_id(parsed.get("tenant_id")),
    }


def _parts(credential: str) -> tuple[str, str]:
    parts = credential.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        _reject_credential("invalid")
    return parts[0], parts[1]


def _account_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _reject_credential("invalid")
    return value


def _aware(now: datetime, message: str) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise AuthenticationError(message)
    return now


def _burn(password: str) -> None:
    try:
        _BURN.verify(_DUMMY_HASH, password)
    except VerificationError:
        return


def _reject_login(reason: str, **fields: object) -> NoReturn:
    logger.debug("login rejected", reason=reason, **fields)
    logger.error("login rejected")
    raise AuthenticationError("login rejected")


def _reject_credential(reason: str) -> NoReturn:
    logger.debug("control credential rejected", reason=reason)
    logger.error("control credential rejected", reason=reason)
    if reason == "expired":
        raise AuthenticationError("control credential expired")
    raise AuthenticationError("control credential rejected")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
