"""Alembic environment checks against the dev database."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text

from alembic import command
from ceho.hot.session import create_database_engine

ROOT = Path(__file__).resolve().parent.parent


def _require_database() -> None:
    load_dotenv()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pytest.skip("DATABASE_URL is not set")
    engine = create_database_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        pytest.skip(f"PostgreSQL is not reachable: {error}")
    finally:
        engine.dispose()


def test_autogenerate_finds_no_drift_outside_mapped_schema() -> None:
    _require_database()
    # Without an ini file, env.py skips fileConfig and leaves test logging alone.
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))

    command.check(config)
