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

RUN groupadd --gid 1001 mediahub \
    && useradd --uid 1001 --gid mediahub --create-home --shell /bin/bash mediahub

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=mediahub:mediahub alembic.ini ./alembic.ini
COPY --chown=mediahub:mediahub migrations ./migrations
COPY --chown=mediahub:mediahub docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data/library /data/staging \
    && chown -R mediahub:mediahub /data

USER mediahub
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as r, sys; sys.exit(0 if r.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["entrypoint.sh"]
CMD ["mediahub"]
