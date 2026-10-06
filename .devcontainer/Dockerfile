FROM mcr.microsoft.com/devcontainers/python:1-3.10-bookworm

# Yarn's apt signing key is unpublished, so drop that source before update.
RUN find /etc/apt/sources.list.d -type f -print0 \
        | xargs -0 grep -l 'dl.yarnpkg.com' \
        | xargs -r rm -f \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy
