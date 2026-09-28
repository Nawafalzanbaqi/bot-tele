#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Rebuild the image so the download engine stays current, and record what happened.
#
# yt-dlp is the one dependency whose correctness expires. Extractors track what
# platforms do, and platforms change without notice: a site that worked last
# month starts failing with a message about being unable to extract, and the
# fix is usually already published upstream.
#
# This rebuilds rather than upgrading in place, on purpose. A downloader that
# rewrites itself on an unattended device is a supply chain with no review step
# and no way to roll back; a rebuilt image is a version you can name and
# revert to. The pins live in the Dockerfile as build arguments; this script
# asks PyPI for the newest yt-dlp (pre-releases included by default - the
# nightly channel carries extractor fixes weeks before the stable tag) and
# gallery-dl, builds with those, restarts only if the engine changed, and
# writes a small JSON record that pi-health reads: it alerts when the record
# is older than 14 days (this job stopped running) or the installed engine is
# not the newest release (this job ran but could not build).
#
# Run from cron, weekly, at a quiet hour. Safe to run when nothing has changed.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/bot-tele}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.pi.yml}"
PROFILES=(--profile localapi --profile vpn)
STATE="${STATE:-$HOME/bot-tele-backups/engine-version.json}"
# pre  = newest version on PyPI including nightly pre-releases (what the image has run since 08-02)
# stable = the latest tagged release only
YTDLP_CHANNEL="${YTDLP_CHANNEL:-pre}"

log() { printf '[refresh] %s\n' "$*"; }

cd "${PROJECT_DIR}"
mkdir -p "$(dirname "${STATE}")"

pypi_latest() { # $1 = package, $2 = channel (pre|stable)
    python3 - "$1" "$2" <<'PY'
import json, re, sys, urllib.request
pkg, channel = sys.argv[1], sys.argv[2]
d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30))
if channel == "stable":
    print(d["info"]["version"]); sys.exit(0)
def key(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:\.dev0)?$", v)
    return tuple(int(x) if x else 0 for x in m.groups()) if m else None
cands = [v for v, files in d["releases"].items() if files and key(v)]
print(max(cands, key=key))
PY
}

record() { # $1 = status, then key=value pairs
    local status="$1"; shift
    python3 - "${STATE}" "${status}" "$@" <<'PY'
import json, sys, datetime
path, status, *pairs = sys.argv[1:]
rec = {"status": status, "checked_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}
for p in pairs:
    k, _, v = p.partition("="); rec[k] = v
json.dump(rec, open(path, "w"), indent=2); print(json.dumps(rec))
PY
}

latest_ytdlp="$(pypi_latest yt-dlp "${YTDLP_CHANNEL}")"
latest_gdl="$(pypi_latest gallery-dl stable)"
before="$(docker compose -f "${COMPOSE_FILE}" exec -T telegram \
    python -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo unknown)"
log "engine before: yt-dlp ${before}; newest on PyPI: yt-dlp ${latest_ytdlp} (${YTDLP_CHANNEL}), gallery-dl ${latest_gdl}"

# --pull refreshes the base image too, which is where security updates for
# FFmpeg and the system libraries arrive.
if ! docker compose -f "${COMPOSE_FILE}" "${PROFILES[@]}" build --pull \
        --build-arg "YTDLP_VERSION=${latest_ytdlp}" \
        --build-arg "GALLERYDL_VERSION=${latest_gdl}" telegram; then
    log "build failed; leaving the running containers untouched"
    record build-failed "ytdlp_installed=${before}" "ytdlp_latest=${latest_ytdlp}" "gallerydl_latest=${latest_gdl}" "channel=${YTDLP_CHANNEL}"
    exit 1
fi

after="$(docker run --rm --entrypoint python mediahub/app:latest \
    -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo unknown)"
gdl_after="$(docker run --rm --entrypoint python mediahub/app:latest \
    -c 'import gallery_dl.version as g; print(g.__version__)' 2>/dev/null || echo unknown)"
deno_after="$(docker run --rm --entrypoint deno mediahub/app:latest --version 2>/dev/null | head -1 || echo unknown)"

restarted=no
if [ "${before}" = "${after}" ]; then
    log "engine unchanged (${after}); not restarting"
else
    log "engine ${before} -> ${after}; restarting telegram + api"
    # Recreating drains first: stop_grace_period is longer than any drain the
    # gateway performs, so a download in flight finishes and is delivered rather
    # than being abandoned half-uploaded. Both services run this image.
    docker compose -f "${COMPOSE_FILE}" "${PROFILES[@]}" up -d telegram api
    restarted=yes
fi

record ok "ytdlp_installed=${after}" "ytdlp_latest=${latest_ytdlp}" "gallerydl_installed=${gdl_after}" \
    "gallerydl_latest=${latest_gdl}" "deno=${deno_after}" "channel=${YTDLP_CHANNEL}" "restarted=${restarted}" \
    "image_id=$(docker image inspect mediahub/app:latest --format '{{.Id}}' 2>/dev/null | cut -c8-19)"
log "done"

# Old image layers accumulate at roughly a gigabyte a rebuild. Dangling only:
# tagged images (mediahub/app:pre-batch) are kept.
docker image prune -f >/dev/null 2>&1 || true
