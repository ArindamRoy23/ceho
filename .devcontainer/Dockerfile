FROM mcr.microsoft.com/devcontainers/python:1-3.10-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy
