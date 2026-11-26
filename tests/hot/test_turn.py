"""Cloudflare STUN/TURN credentials minted for one join."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from ceho.hot.turn import CloudflareTurn, IceRelay, TurnUnavailable

STUN = "stun:stun.cloudflare.com:3478"
TURN = "turns:turn.cloudflare.com:443?transport=tcp"
_BODY = {
    "iceServers": {
        "urls": [STUN, "turn:turn.cloudflare.com:3478?transport=udp", TURN],
        "username": "1700000000:tenant",
        "credential": "short-lived-secret",
    }
}


class _Http:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[tuple[str, Mapping[str, str], bytes]] = []

    def post(self, url: str, headers: Mapping[str, str], payload: bytes, timeout: float) -> bytes:
        self.calls.append((url, headers, payload))
        if self.status != 200:
            raise TurnUnavailable("turn is unavailable")
        assert timeout > 0
        return self.body


def test_mint_uses_configured_cloudflare_servers_and_the_returned_password() -> None:
    http = _Http(json.dumps(_BODY).encode())
    turn = CloudflareTurn("key-1", "api-token", stun_url=STUN, turn_url=TURN, http=http)

    relay = turn.mint(ttl_seconds=60)

    assert relay == IceRelay(
        stun_url=STUN,
        turn_url=TURN,
        username="1700000000:tenant",
        password="short-lived-secret",
    )
    url, headers, payload = http.calls[0]
    assert url == "https://rtc.live.cloudflare.com/v1/turn/keys/key-1/credentials/generate"
    assert headers["Authorization"] == "Bearer api-token"
    assert json.loads(payload) == {"ttl": 60}


def test_mint_reads_a_list_of_ice_servers() -> None:
    body = {
        "iceServers": [
            {"urls": [STUN]},
            {
                "urls": [TURN],
                "username": "user-2",
                "credential": "pass-2",
            },
        ]
    }
    turn = CloudflareTurn(
        "key-1",
        "api-token",
        stun_url=STUN,
        turn_url=TURN,
        http=_Http(json.dumps(body).encode()),
    )

    relay = turn.mint(ttl_seconds=30)

    assert relay.username == "user-2"
    assert relay.password == "pass-2"
    assert relay.stun_url == STUN
    assert relay.turn_url == TURN


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b'{"iceServers": {}}',
        b'{"iceServers": [{"urls": ["stun:stun.cloudflare.com:3478"]}]}',
    ],
)
def test_mint_rejects_a_response_without_a_password(body: bytes) -> None:
    turn = CloudflareTurn("key-1", "api-token", stun_url=STUN, turn_url=TURN, http=_Http(body))

    with pytest.raises(TurnUnavailable, match="turn is unavailable"):
        turn.mint(ttl_seconds=60)


def test_mint_rejects_an_upstream_error() -> None:
    turn = CloudflareTurn(
        "key-1",
        "api-token",
        stun_url=STUN,
        turn_url=TURN,
        http=_Http(b"", status=503),
    )

    with pytest.raises(TurnUnavailable, match="turn is unavailable"):
        turn.mint(ttl_seconds=60)


def test_mint_logs_do_not_carry_the_password_or_the_api_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="ceho.hot.turn")
    http = _Http(json.dumps(_BODY).encode())
    turn = CloudflareTurn("key-1", "api-token", stun_url=STUN, turn_url=TURN, http=http)

    turn.mint(ttl_seconds=60)

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
    assert "short-lived-secret" not in rendered
    assert "api-token" not in rendered
    assert "1700000000:tenant" not in rendered
    assert any(record.msg == "turn credentials minted" for record in caplog.records)
