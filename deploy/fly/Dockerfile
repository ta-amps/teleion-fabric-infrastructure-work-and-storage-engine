FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra server --extra mcp
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
# The service listens on 8080 (fly.toml internal_port); health check GET /v1/health.
CMD ["tessera", "serve", "--host", "0.0.0.0", "--port", "8080"]
