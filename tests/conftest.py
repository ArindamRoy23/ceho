"""Process-wide test fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ceho.logging import reset_logging


@pytest.fixture(autouse=True)
def _reset_process_logging() -> Iterator[None]:
    """Drop handlers left by a test that called ``configure``."""
    yield
    reset_logging()
