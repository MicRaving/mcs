# CodingStudio server — ONE image, ONE product.
#
# The sync server (desktop clients) always runs.  Browser mode (the gateway and
# its per-user headless CodingStudio workers) is a switch on the same image:
#
#   STUDIO_BROWSER_MODE=0   sync server only, ~52 MB idle, 1 core (default)
#   STUDIO_BROWSER_MODE=1   + the browser gateway on /app/ (8780)
#
# Both live in this container's cgroup, so its memory limit bounds the whole
# browser side; the entrypoint derives the worker cap and the pressure ceiling
# from memory.max (override with STUDIO_WEB_MAX_WORKERS /
# STUDIO_WEB_PRESSURE_MB).  With the switch off the browser modules are never
# imported and the browser stack costs nothing.
#
# Build from the REPO ROOT (Server/docker-compose.yml does this):
#   docker build -f Server/Dockerfile -t studio-server:local .
# BuildKit (default in current Docker) reads Server/Dockerfile.dockerignore, an
# allow-list that keeps the build context small (the repo holds large data folders).
FROM python:3.11-slim

# MALLOC_ARENA_MAX=2: a request burst must not ratchet the worker's RSS (see
# Results/LOWMEM_server_assessment_measurements.md §4.3).  The Python allocator
# is left at its default (PYTHONMALLOC is deliberately not set).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    STUDIO_DATA_DIR=/data \
    STUDIO_BROWSER_MODE=0 \
    STUDIO_BROWSER_PORT=8780 \
    STUDIO_WEB_DATA=/srv/studio/web \
    STUDIO_WEB_SERVER=http://127.0.0.1:8770 \
    STUDIO_WEB_LOCAL_BLOBS=/data/blobs \
    PYTHONPATH=/app/Server

# non-root runtime user (fixed UID so host-side ownership can be set by install.sh)
RUN groupadd --system --gid 10001 studio \
 && useradd --system --uid 10001 --gid studio --home-dir /srv/studio/web \
            --no-create-home --shell /usr/sbin/nologin studio \
 && mkdir -p /data/blobs /srv/studio/web \
 && chown -R studio:studio /data /srv/studio/web

WORKDIR /app

COPY Server/requirements-prod.txt Server/requirements-web.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-prod.txt -r /tmp/requirements-web.txt \
 && rm -f /tmp/requirements-*.txt \
 && find /usr/local/lib/python3.11/site-packages -name '__pycache__' -prune -exec rm -rf {} + \
 && find /usr/local/lib/python3.11/site-packages -name '*.pyc' -delete

# Application code, owned by root and read-only for the runtime user.  The repo
# layout is kept, so `studio_web.repo_scripts()` finds
# /app/Scripts/coding_web/browser_worker.py and the worker's import path is
# /app/Scripts + /app/Scripts/coding_web (exactly as in a checkout).
# `Scripts/coding_web/Schemes/` travels with the app: the scheme library is
# vocabulary, never project data, and it is read-only here (scheme editing is
# desktop-only, PLAN_online_only §7).
COPY Scripts/ /app/Scripts/
COPY Server/studio_server /app/Server/studio_server
COPY Server/studio_web /app/Server/studio_web

WORKDIR /app/Server
USER studio
EXPOSE 8770 8780

# the sync server's own health (the browser side is reported inside it)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4).status == 200 else 1)"

# PID 1: the sync server, plus the browser gateway when STUDIO_BROWSER_MODE=1.
# The entrypoint forwards SIGTERM, reaps orphaned workers, restarts a crashed
# gateway and exits non-zero when the sync server itself cannot start.
CMD ["python", "-m", "studio_server", "run", "--host", "0.0.0.0", "--port", "8770", "--data", "/data"]
