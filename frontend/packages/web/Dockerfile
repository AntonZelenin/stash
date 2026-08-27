# Dockerfile for the web frontend.
#
# Runs `dx serve` in dev mode. Build context is the `frontend/` directory so
# the whole cargo workspace (ui, web, ...) is available.
#
# A production build (`dx build --release` + nginx) was tried here and
# reverted: it broke the app in Firefox (page stuck on the loading screen,
# no console error) for a reason not yet root-caused, even after ruling out
# wasm-opt. This dev-mode server is slower to start (~60-80s full compile on
# every `docker compose up`, since there's no warm `target/` cache in the
# container) but is confirmed working in both Chrome and Firefox.

FROM rust:1-bookworm

RUN rustup target add wasm32-unknown-unknown \
    && cargo install dioxus-cli --version "^0.7" --locked

WORKDIR /app
COPY . .
WORKDIR /app/packages/web

EXPOSE 8080

# --watch/--hot-reload are off: this is a static COPY snapshot with no bind
# mount, so there's nothing for the filesystem watcher to legitimately catch.
# todo: this is a dev server, prod server did not work on Firefox!
CMD ["dx", "serve", "--platform", "web", "--addr", "0.0.0.0", "--port", "8080", "--open", "false", "--interactive", "false", "--watch", "false", "--hot-reload", "false"]
