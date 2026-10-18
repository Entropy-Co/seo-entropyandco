# Framer's Server API is a Node package, so the image carries a Node runtime for
# framer-bridge/ next to the Python engine.
FROM node:22-bookworm-slim AS bridge
WORKDIR /bridge
RUN corepack enable
COPY framer-bridge/package.json framer-bridge/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --prod
COPY framer-bridge/bridge.mjs ./

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VIRTUAL_ENV=/app/.venv PATH="/app/.venv/bin:$PATH"
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode
COPY config/ ./config/
COPY --from=bridge /bridge ./framer-bridge
ENTRYPOINT ["entropyseo"]
CMD ["--help"]
