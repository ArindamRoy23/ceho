FROM mcr.microsoft.com/devcontainers/python:1-3.10-bookworm

# Yarn's apt signing key is unpublished, so drop that source before update.
# GitHub CLI comes from the official apt repository.
RUN find /etc/apt/sources.list.d -type f -print0 \
        | xargs -0 grep -l 'dl.yarnpkg.com' \
        | xargs -r rm -f \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client gh \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy
