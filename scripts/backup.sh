#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Back up the SQLite system of record.
#
# **Never copy a live WAL database with cp.** The result opens, looks fine, and
# is missing its most recent transactions - the failure only shows up on the
# day the backup is needed. SQLite's own backup API takes a consistent snapshot
# of a database that is being written to, which is what this uses.
#
# Run from cron. Keeps the most recent KEEP backups and deletes the rest, so it
# cannot fill the disk it is protecting.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/bot-tele}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.pi.yml}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/bot-tele-backups}"
KEEP="${KEEP:-14}"
SERVICE="${SERVICE:-telegram}"

log() { printf '[backup] %s\n' "$*"; }

cd "${PROJECT_DIR}"
mkdir -p "${BACKUP_DIR}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="${BACKUP_DIR}/mediahub-${stamp}.db"

if ! docker compose -f "${COMPOSE_FILE}" ps --status running --services | grep -qx "${SERVICE}"; then
    log "service '${SERVICE}' is not running; nothing to back up"
    exit 0
fi

# The snapshot is written inside the container, then copied out. Writing
# straight to a bind mount would need one, and this works with a named volume.
docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE}" python - <<'PY'
import sqlite3

source = sqlite3.connect("/data/mediahub.db")
target = sqlite3.connect("/tmp/backup.db")
with target:
    source.backup(target)
target.close()
source.close()
PY

docker compose -f "${COMPOSE_FILE}" cp "${SERVICE}:/tmp/backup.db" "${target}"
docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE}" rm -f /tmp/backup.db

# Verify what was written rather than trusting that it was. A backup nobody has
# opened is a hope, not a backup.
if ! sqlite3 "${target}" "PRAGMA integrity_check;" 2>/dev/null | grep -qx "ok"; then
    if command -v sqlite3 >/dev/null 2>&1; then
        log "FAILED: the copy did not pass an integrity check"
        rm -f "${target}"
        exit 1
    fi
    log "sqlite3 is not installed here; skipping the integrity check"
fi

log "wrote ${target} ($(du -h "${target}" | cut -f1))"

# Prune oldest first, keeping KEEP.
mapfile -t old < <(ls -1t "${BACKUP_DIR}"/mediahub-*.db 2>/dev/null | tail -n "+$((KEEP + 1))")
for file in "${old[@]:-}"; do
    [ -n "${file}" ] || continue
    rm -f "${file}"
    log "pruned $(basename "${file}")"
done
