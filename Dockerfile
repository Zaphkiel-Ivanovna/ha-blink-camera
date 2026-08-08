ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.06.1
FROM ${BUILD_FROM}

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

COPY rootfs /

LABEL \
    org.opencontainers.image.title="Blink Camera Streamer" \
    org.opencontainers.image.description="Re-broadcasts a Blink camera liveview as MPEG-TS on the LAN" \
    org.opencontainers.image.source="https://github.com/Zaphkiel-Ivanovna/ha-blink-camera" \
    org.opencontainers.image.licenses="MIT"
