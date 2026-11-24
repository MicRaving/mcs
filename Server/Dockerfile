# Studio server image.
# Build from the REPO ROOT (Server/docker-compose.yml does this):
#   docker build -f Server/Dockerfile -t studio-server:local .
# BuildKit (default in current Docker) reads Server/Dockerfile.dockerignore, an
# allow-list that keeps the build context small (the repo holds large data folders).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STUDIO_DATA_DIR=/data

# non-root runtime user (fixed UID so host-side ownership can be set by install.sh)
RUN groupadd --system --gid 10001 studio \
 && useradd --system --uid 10001 --gid studio --home-dir /app --no-create-home --shell /usr/sbin/nologin studio \
 && mkdir -p /data \
 && chown studio:studio /data

WORKDIR /app

COPY Server/requirements-prod.txt /tmp/requirements-prod.txt
RUN pip install --no-cache-dir -r /tmp/requirements-prod.txt \
 && rm -f /tmp/requirements-prod.txt

# application code: owned by root, read-only for the runtime user
COPY Scripts/online_shared /app/online_shared
COPY Server/studio_server /app/studio_server

USER studio
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4).status == 200 else 1)"

# single process, single worker: keeps the memory footprint small
CMD ["python", "-m", "studio_server", "serve", "--host", "0.0.0.0", "--port", "8770", "--data", "/data"]
