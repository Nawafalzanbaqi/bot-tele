# syntax=docker/dockerfile:1.9
# ---------------------------------------------------------------------------
# MediaHub image.
#
# Stages:
#   builder  - compiles dependencies into an isolated virtualenv
#   dev      - editable install + dev tooling, used by docker-compose.override
#   runtime  - minimal, non-root production image
#
# The virtualenv is built once and copied verbatim into the runtime stage so
# that no build toolchain ships to production.
# ---------------------------------------------------------------------------

ARG PYTHON_VERSION=3.13

# Engine pins. scripts/refresh.sh asks PyPI for the newest yt-dlp (pre-releases included -
# the nightly channel carries extractor fixes weeks before the stable tag) and gallery-dl,
# passes them as --build-arg and rebuilds. The image stays the unit of change: nothing
# updates itself at runtime.
ARG YTDLP_VERSION=2026.9.27.232945.dev0
ARG GALLERYDL_VERSION=1.32.14
# yt-dlp >= 2025.11 wants a JavaScript runtime for YouTube's signature/n-challenge scripts
# and warns that extraction without one "has been deprecated, and some formats may be
# missing". Measured on this Pi on 2026-09-28 the format list of a 1080p video was still
# complete without it, so this is insurance against the deprecation landing, not a fix for
# a live regression. Pinned like everything else.
ARG DENO_VERSION=v2.9.7

# --------------------------------------------------------------------------- #
# Builder                                                                      #
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}"

WORKDIR /build

# Copy only what the build backend needs, so dependency layers stay cached.
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install .

# yt-dlp separately, and from the pre-release channel, because it is the one
# dependency whose correctness expires. Extractors track what platforms do, and
# platforms change without notice: a month-old release fails on TikTok with
# "unable to extract" and the fix is already published. Pinning it to the
# quarterly stable means the bot is broken for weeks at a time on exactly the
# sites people use most.
#
# It is still *pinned by the image*, never self-updating at runtime - a
# downloader that rewrites itself on an unattended device is a supply chain with
# no review step. Rebuilding is the update mechanism.
#
# The `curl-cffi` extra is not optional in practice. Without it yt-dlp has **no
# impersonation targets at all**, and a growing number of extractors simply
# refuse to run: Dailymotion says so outright, and TikTok, Instagram and
# Facebook sit behind bot walls that fingerprint the TLS handshake and the
# HTTP/2 settings frame. A plain Python client is identifiable no matter what
# User-Agent it claims, which is why those sites answer with a challenge page
# instead of the media. curl_cffi presents a real browser's fingerprint, and it
# is the difference between "unable to extract" and a download.
#
# PySocks alongside it, so a `socks5://` proxy works. yt-dlp accepts the URL
# either way and only discovers the missing library when a download is already
# under way, which turns a one-line configuration mistake into an intermittent
# runtime failure.
ARG YTDLP_VERSION
ARG GALLERYDL_VERSION
RUN python -m pip install "yt-dlp[default,curl-cffi]==${YTDLP_VERSION}" \
        "gallery-dl==${GALLERYDL_VERSION}" "PySocks>=1.7.1" \
    && python -c 'import yt_dlp, gallery_dl.version as g; print("yt-dlp", yt_dlp.version.__version__, "gallery-dl", g.__version__)'

# deno: the JavaScript runtime yt-dlp runs YouTube's challenge scripts in. Fetched from the
# official release with its published SHA-256 checked, so the pin above is the whole story.
ARG DENO_VERSION
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH:-arm64}" in \
      arm64) triple=aarch64-unknown-linux-gnu ;; \
      amd64) triple=x86_64-unknown-linux-gnu ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/denoland/deno/releases/download/${DENO_VERSION}"; \
    curl -fsSL "${base}/deno-${triple}.zip" -o /tmp/deno.zip; \
    curl -fsSL "${base}/deno-${triple}.zip.sha256sum" -o /tmp/deno.sha256; \
    (cd /tmp && awk '{print $1"  deno.zip"}' deno.sha256 | sha256sum -c -); \
    unzip -q /tmp/deno.zip -d /usr/local/bin; \
    chmod 755 /usr/local/bin/deno; \
    rm -f /tmp/deno.zip /tmp/deno.sha256; \
    /usr/local/bin/deno --version

# --------------------------------------------------------------------------- #
# Development                                                                  #
# --------------------------------------------------------------------------- #
FROM builder AS dev

ENV MEDIAHUB_ENVIRONMENT=local

WORKDIR /app
COPY . .
RUN python -m pip install -e ".[dev]"

EXPOSE 8000
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["entrypoint.sh"]
CMD ["uvicorn", "mediahub.presentation.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]

# --------------------------------------------------------------------------- #
# Runtime                                                                      #
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    MEDIAHUB_ENVIRONMENT=production

# FFmpeg is what makes the high qualities reachable at all. Above roughly 720p
# every large platform ships video and audio as separate streams, so "1080p"
# means "fetch two files and mux them" - without a merger the engine silently
# falls back to the best already-muxed rendition and the user gets 720p having
# asked for 1080p. From the distribution rather than a static build, so it gets
# security updates through a base image rebuild
# (docs/architecture/18-deployment-architecture.md §18.2).
#
# aria2 is an optional external downloader for yt-dlp (many connections per file). It is in
# the image so the choice can be made by measurement and flipped by configuration.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg aria2 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1001 mediahub \
    && useradd --uid 1001 --gid mediahub --create-home --shell /bin/bash mediahub

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/bin/deno /usr/local/bin/deno
RUN deno --version && aria2c --version | head -1

ARG YTDLP_VERSION
ARG GALLERYDL_VERSION
ARG DENO_VERSION
LABEL mediahub.engine.ytdlp="${YTDLP_VERSION}" \
      mediahub.engine.gallerydl="${GALLERYDL_VERSION}" \
      mediahub.engine.deno="${DENO_VERSION}"

WORKDIR /app
COPY --chown=mediahub:mediahub alembic.ini ./alembic.ini
COPY --chown=mediahub:mediahub migrations ./migrations
COPY --chown=mediahub:mediahub docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data/workspace \
    && chown -R mediahub:mediahub /data

USER mediahub
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as r, sys; sys.exit(0 if r.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["mediahub"]
