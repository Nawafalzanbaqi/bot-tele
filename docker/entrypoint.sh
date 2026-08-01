#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Container entrypoint.
#
# Responsibilities (deliberately minimal - the app owns everything else):
#   1. Optionally apply database migrations before the process starts.
#   2. Exec the requested command as PID 1 so signals reach the app.
#
# Controlled by:
#   MEDIAHUB_RUN_MIGRATIONS  true|false (default: false)
# ---------------------------------------------------------------------------
set -Eeuo pipefail

RUN_MIGRATIONS="${MEDIAHUB_RUN_MIGRATIONS:-false}"
BACKEND="${MEDIAHUB_DATABASE__BACKEND:-postgres}"

log() {
    printf '[entrypoint] %s\n' "$*" >&2
}

if [[ "${RUN_MIGRATIONS}" == "true" && "${BACKEND}" == "postgres" ]]; then
    log "applying database migrations (alembic upgrade head)"
    alembic upgrade head
    log "migrations applied"
else
    log "skipping migrations (MEDIAHUB_RUN_MIGRATIONS=${RUN_MIGRATIONS}, backend=${BACKEND})"
fi

log "starting: $*"
exec "$@"
