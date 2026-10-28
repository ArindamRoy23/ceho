# Local development environment

Refs #1.

This page records the current local environment. It is not a product architecture decision. The data stack will be finalized after the design discussion.

## What is set up

The devcontainer in `.devcontainer/` runs the app container on Python 3.10 with [uv](https://docs.astral.sh/uv/), matching `.python-version`. The workspace is `/workspaces/ceho`. `postCreateCommand` runs `uv sync` into `/home/vscode/.venv`, so the container environment does not replace a `.venv` created on the host.

PostgreSQL 16 is a second Compose service, used only so local work has a SQL database. Credentials in `.env` are development defaults (`ceho` / `ceho` on database `ceho`). They are not production secrets. `.env` is gitignored. `.env.example` is the template, and the devcontainer copies it to `.env` when the file is missing. Compose loads that same `.env` into the app and Postgres containers and uses it for variable substitution. `COMPOSE_FILE` in `.env` points at `.devcontainer/docker-compose.yml`. `dockerComposeFile` is empty so the devcontainer substitutes variables from this repo-root file.

| Setting | Inside the devcontainer | From the host |
| --- | --- | --- |
| Host | `postgres` | `localhost` |
| Port | `5432` | `5433` (published; set `POSTGRES_HOST_PORT` to change it) |
| URL | `postgresql://ceho:ceho@postgres:5432/ceho` | `postgresql://ceho:ceho@localhost:5433/ceho` |

`.env` uses the in-network host `postgres`. From the host machine, point `DATABASE_URL` at `localhost` and `POSTGRES_HOST_PORT` instead.

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

## Hot-server foundation

The hot-server ASGI factory is `ceho.hot.server:create_app`. After copying
`.env.example` to `.env`, start the server from the repo root:

```bash
uv run python -m ceho.hot.server
```

`HotServerSettings.from_environment` loads that `.env` when it reads the
process environment. Variables already set in the process stay in place.
Modules take a `HotServerSettings` value from that read.

The server listens on `CEHO_HOST` and `CEHO_PORT` (default `127.0.0.1:8000`
when unset) and exposes `GET /health`. It validates `DATABASE_URL`,
`CEHO_MEDIA_STORAGE_URI`, `CEHO_SIGNALING_SECRET`, and
`CEHO_EVENT_PUBLISHER_URL` before startup. The sample media URI and in-memory
publisher are development-only placeholders; this foundation does not record
audio or publish events yet.
