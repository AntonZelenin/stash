# Dockerfile for the content-analyzer processing worker.
#
# Build context is the `backend/` directory (see docker-compose.yml), so all
# paths below are relative to `backend/`.

# ---- builder: resolve + install dependencies into a venv -------------------
FROM python:3.14-slim AS builder

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# `stash-shared` (the queue interface + Valkey client) is installed first,
# same reasoning as the api image: no lockfile/workspace tooling here, so
# each service's image installs it as a plain local package.
COPY shared ./shared
COPY content_analyzer/pyproject.toml ./content_analyzer/pyproject.toml
COPY content_analyzer/src ./content_analyzer/src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ./shared \
    && pip install --no-cache-dir ./content_analyzer

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

CMD ["python", "-m", "content_analyzer.main"]
