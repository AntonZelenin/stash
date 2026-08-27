# Dockerfile for the API service.
#
# Build context is the `backend/` directory (see docker-compose.yml), so all
# paths below are relative to `backend/`.

# ---- builder: resolve + install dependencies into a venv -------------------
FROM python:3.14-slim AS builder

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copying pyproject.toml + src together (rather than deps first) is the
# honest tradeoff here: there's no lockfile, and hatchling needs the actual
# package present to resolve/build the wheel, so a deps-only cache layer
# isn't achievable without extra tooling. Not worth it for a service this
# small.
COPY api/pyproject.toml ./
COPY api/src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# ---- runtime -----------------------------------------------------------
FROM python:3.14-slim AS runtime

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# The venv already has `app` installed as a real package (via pyproject.toml's
# packages = ["src/app"]), so only the Alembic config/migrations need to be
# copied separately — the application source itself doesn't.
COPY --from=builder /opt/venv /opt/venv
COPY api/alembic.ini ./
COPY api/alembic ./alembic
COPY api/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chown -R app:app /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health', timeout=2)"

ENTRYPOINT ["docker-entrypoint.sh"]
