FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV VEXIC_HOSTED_ROOT=/data/vexic

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY adapters ./adapters

RUN uv sync --frozen --no-dev --extra hosted
RUN useradd --uid 10001 --create-home --user-group --shell /usr/sbin/nologin vexic \
    && mkdir -p /data/vexic \
    && chown -R vexic:vexic /data/vexic

ENV PYTHONPATH=/app/src:/app
RUN PYTHONPATH="/app/src:/app${PYTHONPATH:+:$PYTHONPATH}" uv run --no-sync python -c "from uvicorn.importer import import_from_string; import_from_string('vexic.hosted_control_plane_http:create_app'); import adapters.turso_adapter"

EXPOSE 8000

# nosemgrep: root startup repairs the Railway volume, then hosted_entrypoint drops to vexic.
USER root

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=3).read()"

CMD ["/app/.venv/bin/python", "-m", "vexic.hosted_entrypoint"]
