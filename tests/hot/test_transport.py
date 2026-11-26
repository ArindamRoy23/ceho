"""Contracts for one direct-source transport queue and its binding."""

from datetime import datetime, timezone

import pytest

from ceho.hot.config import ConfigurationError, TransportQueueConfig
from ceho.hot.transport import AuthorizedSessionBinding, TransportError


def test_transport_queue_defaults_to_a_bounded_frame_queue() -> None:
    config = TransportQueueConfig()

    assert config.max_queued_frames == 50
    assert config.max_frame_age_ms == 1_000
    assert config.max_frame_bytes == 64 * 1024


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_queued_frames": 0}, "transport queue must hold at least one frame"),
        ({"max_frame_age_ms": 0}, "transport frame age must be at least 1 ms"),
        ({"max_frame_bytes": 0}, "transport frame size must be at least 1 byte"),
    ],
)
def test_transport_queue_bounds_fail_clearly(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        TransportQueueConfig(**kwargs)


def test_binding_names_an_authorized_peer() -> None:
    expires_at = datetime(2026, 11, 26, tzinfo=timezone.utc)

    binding = AuthorizedSessionBinding(
        tenant_id=1,
        session_id=10,
        peer_id="peer-1",
        expires_at=expires_at,
    )

    assert binding.tenant_id == 1
    assert binding.session_id == 10
    assert binding.peer_id == "peer-1"
    assert binding.expires_at == expires_at


@pytest.mark.parametrize(
    ("tenant_id", "session_id", "peer_id"),
    [
        (0, 10, "peer-1"),
        (1, 0, "peer-1"),
        (1, 10, ""),
        (1, 10, " peer-1"),
        (1, 10, "peer/1"),
        (1, 10, "peer\\1"),
    ],
)
def test_binding_rejects_an_unnamed_peer(tenant_id: int, session_id: int, peer_id: str) -> None:
    with pytest.raises(TransportError, match="binding must name a tenant, session, and peer"):
        AuthorizedSessionBinding(
            tenant_id=tenant_id,
            session_id=session_id,
            peer_id=peer_id,
            expires_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
        )


def test_binding_expiry_must_be_timezone_aware() -> None:
    with pytest.raises(TransportError, match="binding expiry must be timezone-aware"):
        AuthorizedSessionBinding(
            tenant_id=1,
            session_id=10,
            peer_id="peer-1",
            expires_at=datetime(2026, 11, 26),
        )
