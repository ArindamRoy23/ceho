#!/bin/sh
# Fail when ceho/models.py changes without an Alembic revision in the same change.
set -eu

if [ "${1:-}" = "staged" ]; then
    models=$(git diff --cached --name-only -- ceho/models.py)
    versions=$(git diff --cached --name-only -- alembic/versions)
elif [ "$#" -eq 2 ]; then
    models=$(git diff --name-only "$1" "$2" -- ceho/models.py)
    versions=$(git diff --name-only "$1" "$2" -- alembic/versions)
else
    echo "usage: check_model_revision.sh staged | <base-sha> <head-sha>" >&2
    exit 2
fi

if [ -n "$models" ] && [ -z "$versions" ]; then
    cat <<'EOF' >&2
ceho/models.py changed without an Alembic revision.

Generate one, review it, and include it in this change:

  uv run alembic revision --autogenerate -m "describe the schema change"

Autogenerate can omit triggers, composite foreign keys, ON DELETE SET NULL (user_id), and check constraints. Add those by hand before applying the revision.
EOF
    exit 1
fi
