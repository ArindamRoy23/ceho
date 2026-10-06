# Local development environment

Refs #1.

This page records the current local environment. It is not a product architecture decision. The data stack will be finalized after the design discussion.

## What is set up

The devcontainer in `.devcontainer/` runs the app container on Python 3.10 with [uv](https://docs.astral.sh/uv/), matching `.python-version`. `postCreateCommand` runs `uv sync`.

PostgreSQL 16 is a second Compose service, used only so local work has a SQL database. Credentials in `.env.example` and the Compose file are development defaults (`ceho` / `ceho` on database `ceho`). They are not production secrets.

| Setting | Inside the devcontainer | From the host |
| --- | --- | --- |
| Host | `postgres` | `localhost` |
| Port | `5432` | `5433` (published; set `POSTGRES_HOST_PORT` to change it) |
| URL | `postgresql://ceho:ceho@postgres:5432/ceho` | `postgresql://ceho:ceho@localhost:5433/ceho` |

Copy `.env.example` to `.env` before using the URL from the host. The devcontainer already sets `DATABASE_URL` to the in-network host.

## What is not set up

These were considered for the local stack and left unset:

- Vector database
- Cache (including Redis)
- Queue (including Kafka)

## Checks

GitHub Actions (`.github/workflows/ci.yml`) runs on pull requests and on pushes to `main`:

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy .`

Ruff and mypy are development dependencies. mypy checks function bodies (`check_untyped_defs`) without requiring annotations on every function yet.
