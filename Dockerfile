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
    && apt-get install --no-install-recommends -y build-essential \
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
RUN python -m pip install --upgrade --pre yt-dlp

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
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1001 mediahub \
    && useradd --uid 1001 --gid mediahub --create-home --shell /bin/bash mediahub

COPY --from=builder /opt/venv /opt/venv

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
