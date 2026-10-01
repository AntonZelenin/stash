# Dockerfile for the video_analyzer worker.
#
# Build context is the `backend/` directory (see docker-compose.yml), so all
# paths below are relative to `backend/`. Only this worker, the worker core
# and `stash-shared` go in: a change to another worker never changes this
# image.

# ---- ffmpeg: the same static build the Lambda layer has -----------------
# The binary bundled in the imageio-ffmpeg wheel, pinned by hash (keep in
# step with FFMPEG_WHEEL in scripts/build_lambda_packages.py). Only the
# binary is kept; the Python package isn't installed in the image.
FROM python:3.14-slim AS ffmpeg

RUN printf '%s\n' \
        'imageio-ffmpeg==0.6.0 \' \
        '    --hash=sha256:c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282 \' \
        '    --hash=sha256:1d47bebd83d2c5fc770720d211855f208af8a596c82d17730aa51e815cdee6dc' \
        > /tmp/requirements.txt \
    && pip install --no-cache-dir --require-hashes --only-binary=:all: --target /tmp/wheel -r /tmp/requirements.txt \
    && cp /tmp/wheel/imageio_ffmpeg/binaries/ffmpeg-linux-* /usr/local/bin/ffmpeg \
    && chmod 0755 /usr/local/bin/ffmpeg \
    && /usr/local/bin/ffmpeg -hide_banner -version

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
COPY workers/video_analyzer/pyproject.toml ./workers/video_analyzer/pyproject.toml
COPY workers/video_analyzer/src ./workers/video_analyzer/src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ./shared \
    && pip install --no-cache-dir ./workers/core \
    && pip install --no-cache-dir ./workers/video_analyzer

# ---- runtime -----------------------------------------------------------
FROM python:3.14-slim AS runtime

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FFMPEG_PATH=/usr/local/bin/ffmpeg

USER app

CMD ["python", "-m", "video_analyzer"]
