FROM node:24-bookworm-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/tsconfig.app.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
RUN npm ci --no-audit --no-fund
RUN npm run build

FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV NIRQ_STATIC_DIR=/app/frontend/dist
ENV PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist
RUN uv sync --frozen --no-dev --extra web \
    && mkdir -p /data /workspace/imports /workspace/exports \
    && chown -R 10001:10001 /data /workspace
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"]
CMD ["nirq", "web", "--host", "0.0.0.0", "--port", "8000"]
