"""Short-lived Cloudflare Realtime TURN credentials.

The STUN and TURN URLs come from the process environment. The username and
password are minted for one join and are not the CEHO password. They are not
log fields.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from ceho.logging import get_logger

logger = get_logger(__name__)

_GENERATE = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate"
_TIMEOUT_SECONDS = 5.0


class TurnUnavailable(Exception):
    """Cloudflare did not return short-lived TURN credentials."""


class TurnMint(Protocol):
    """Mint one short-lived relay. Tests supply a stand-in."""

    def mint(self, *, ttl_seconds: int) -> IceRelay:
        """Return the STUN URL, TURN URL, and a username and password."""


class TurnHttp(Protocol):
    """POST one credential request. Tests supply a stand-in."""

    def post(self, url: str, headers: Mapping[str, str], payload: bytes, timeout: float) -> bytes:
        """Return the response body."""


@dataclass(frozen=True)
class IceRelay:
    """ICE servers for one browser and the matching hot-server peer."""

    stun_url: str
    turn_url: str
    username: str
    password: str


class CloudflareTurn:
    """Mint a username and password from Cloudflare Realtime TURN."""

    def __init__(
        self,
        key_id: str,
        api_token: str,
        *,
        stun_url: str,
        turn_url: str,
        http: TurnHttp | None = None,
    ) -> None:
        self._key_id = key_id
        self._api_token = api_token
        self._stun_url = stun_url
        self._turn_url = turn_url
        self._http = http or _UrlOpen()

    def mint(self, *, ttl_seconds: int) -> IceRelay:
        """Ask Cloudflare for a credential and bind it to the configured URLs."""
        logger.debug("turn credentials requested", key_id=self._key_id, ttl_seconds=ttl_seconds)
        payload = json.dumps({"ttl": ttl_seconds}).encode()
        url = _GENERATE.format(key_id=quote(self._key_id, safe=""))
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }
        try:
            raw = self._http.post(url, headers, payload, _TIMEOUT_SECONDS)
        except TurnUnavailable:
            logger.error("turn credentials rejected", key_id=self._key_id)
            raise
        except Exception:
            logger.exception("turn credentials failed", key_id=self._key_id)
            raise TurnUnavailable("turn is unavailable") from None
        try:
            username, password = _password(json.loads(raw))
        except (ValueError, json.JSONDecodeError, UnicodeError):
            logger.error("turn credentials rejected", key_id=self._key_id)
            raise TurnUnavailable("turn is unavailable") from None
        relay = IceRelay(
            stun_url=self._stun_url,
            turn_url=self._turn_url,
            username=username,
            password=password,
        )
        logger.info(
            "turn credentials minted",
            key_id=self._key_id,
            stun_url=relay.stun_url,
            turn_url=relay.turn_url,
            ttl_seconds=ttl_seconds,
        )
        return relay


class _UrlOpen:
    def post(self, url: str, headers: Mapping[str, str], payload: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(url, data=payload, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return bytes(response.read())
        except urllib.error.HTTPError as error:
            raise TurnUnavailable("turn is unavailable") from error
        except urllib.error.URLError as error:
            raise TurnUnavailable("turn is unavailable") from error


def _password(payload: object) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("missing")
    ice = payload.get("iceServers")
    if isinstance(ice, dict):
        return _pair(ice)
    if isinstance(ice, list):
        for item in ice:
            if isinstance(item, dict) and item.get("username") and item.get("credential"):
                return _pair(item)
    raise ValueError("missing")


def _pair(item: Mapping[str, object]) -> tuple[str, str]:
    username = item.get("username")
    password = item.get("credential")
    if not isinstance(username, str) or not username:
        raise ValueError("missing")
    if not isinstance(password, str) or not password:
        raise ValueError("missing")
    return username, password
