# GHAADD web viewer: only ghaadd_viewer.py (standard library, no packages). The daemon is NOT run in Docker.
# Build (from this folder):  docker build -t ghaadd-viewer .      Run: see docker-compose.yml
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Run as an unprivileged user; the viewer needs no files, only its port.
RUN useradd --system --no-create-home --uid 10001 viewer

WORKDIR /app
COPY ghaadd_viewer.py /app/ghaadd_viewer.py
USER viewer

# GHAADD_VIEWER_TOKENS (required, comma separated), GHAADD_VIEWER_PORT, GHAADD_VIEWER_HOST, GHAADD_VIEWER_LOST_AFTER
ENV GHAADD_VIEWER_PORT=8888
EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('GHAADD_VIEWER_PORT', '8888'), timeout=4)"

CMD ["python", "/app/ghaadd_viewer.py"]
