FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

RUN useradd --create-home app
USER app

# Railway and most hosts pass the port in $PORT; main() reads it.
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0
EXPOSE 8000
CMD ["nederland-mcp", "--http"]
