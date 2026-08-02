#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Rebuild the image so the download engine stays current.
#
# yt-dlp is the one dependency whose correctness expires. Extractors track what
# platforms do, and platforms change without notice: a site that worked last
# month starts failing with a message about being unable to extract, and the
# fix is usually already published upstream.
#
# This rebuilds rather than upgrading in place, on purpose. A downloader that
# rewrites itself on an unattended device is a supply chain with no review step
# and no way to roll back; a rebuilt image is a version you can name and
# revert to.
#
# Run from cron, weekly, at a quiet hour. Safe to run when nothing has changed:
# Docker's cache makes that a no-op and the containers are left alone.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/bot-tele}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.pi.yml}"
PROFILE="${PROFILE:-localapi}"

log() { printf '[refresh] %s\n' "$*"; }

cd "${PROJECT_DIR}"

before="$(docker compose -f "${COMPOSE_FILE}" exec -T telegram \
    python -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo unknown)"
log "engine before: ${before}"

# --pull refreshes the base image too, which is where security updates for
# FFmpeg and the system libraries arrive.
if ! docker compose -f "${COMPOSE_FILE}" --profile "${PROFILE}" build --pull telegram; then
    log "build failed; leaving the running containers untouched"
    exit 1
fi

after="$(docker run --rm --entrypoint python mediahub/app:latest \
    -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo unknown)"

if [ "${before}" = "${after}" ]; then
    log "engine unchanged (${after}); not restarting"
    exit 0
fi

log "engine ${before} -> ${after}; restarting"
# Recreating drains first: stop_grace_period is longer than any drain the
# gateway performs, so a download in flight finishes and is delivered rather
# than being abandoned half-uploaded.
docker compose -f "${COMPOSE_FILE}" --profile "${PROFILE}" up -d telegram
log "done"

# Old image layers accumulate at roughly a gigabyte a rebuild.
docker image prune -f >/dev/null 2>&1 || true
