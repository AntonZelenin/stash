# Dockerfile for the web frontend.
#
# Runs `dx serve` in dev mode. docker-compose.yml bind-mounts the frontend/
# source over /app at runtime, so the CLI's own filesystem watcher rebuilds
# automatically on host edits — no image rebuild needed for day-to-day
# frontend changes (only Cargo.toml/Dockerfile changes still need one).
# Build context is the `frontend/` directory so the whole cargo workspace
# (ui, web, ...) is available for the image's own baked-in COPY (used when
# the container runs without that bind mount, e.g. in CI).
#
# A production build (`dx build --release` + nginx) was tried here and
# reverted: it broke the app in Firefox (page stuck on the loading screen,
# no console error) for a reason not yet root-caused, even after ruling out
# wasm-opt. This dev-mode server is slower to start (~60-80s full compile on
# first run, until the frontend_target volume warms up) but is confirmed
# working in both Chrome and Firefox.

FROM rust:1-bookworm

RUN rustup target add wasm32-unknown-unknown \
    && cargo install dioxus-cli --version "^0.7" --locked

WORKDIR /app
COPY . .
WORKDIR /app/packages/web

EXPOSE 8080

# --wsl-file-poll-interval: Docker Desktop on Windows runs containers via
# WSL2, and native inotify events for a Windows-host bind mount don't
# reliably cross that boundary — polling is the dependable option here.
# todo: this is the dev server
CMD ["dx", "serve", "--platform", "web", "--addr", "0.0.0.0", "--port", "8080", "--open", "false", "--interactive", "false", "--wsl-file-poll-interval", "2"]
