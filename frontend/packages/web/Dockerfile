# Dockerfile for the web frontend.
#
# Runs `dx serve` in dev mode. Build context is the `frontend/` directory so
# the whole cargo workspace (ui, web, ...) is available.

FROM rust:1-bookworm

RUN rustup target add wasm32-unknown-unknown \
    && cargo install dioxus-cli --version "^0.7" --locked

WORKDIR /app
COPY . .
WORKDIR /app/packages/web

EXPOSE 8080

CMD ["dx", "serve", "--platform", "web", "--addr", "0.0.0.0", "--port", "8080", "--open", "false", "--interactive", "false"]
