# Dockerfile for the image_analyzer worker.
#
# Build context is the `backend/` directory (see docker-compose.yml), so all
# paths below are relative to `backend/`. Only this worker, the worker core
# and `stash-shared` go in: a change to another worker never changes this
# image.

# ---- builder: resolve + install dependencies into a venv -------------------
FROM python:3.14-slim AS builder

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Local packages are installed by path, dependencies first: no
# lockfile/workspace tooling here (same as the api image).
COPY shared ./shared
COPY workers/core/pyproject.toml ./workers/core/pyproject.toml
COPY workers/core/src ./workers/core/src
COPY workers/image_analyzer/pyproject.toml ./workers/image_analyzer/pyproject.toml
COPY workers/image_analyzer/src ./workers/image_analyzer/src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ./shared \
    && pip install --no-cache-dir ./workers/core \
    && pip install --no-cache-dir ./workers/image_analyzer

# ---- runtime -----------------------------------------------------------
FROM python:3.14-slim AS runtime

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

CMD ["python", "-m", "image_analyzer"]
