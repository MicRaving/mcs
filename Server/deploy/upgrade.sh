#!/usr/bin/env bash
# upgrade.sh - upgrade an EXISTING studio CT to a new source tarball.
#
#   bash /opt/studio/Server/deploy/upgrade.sh /root/studio-src.tar.gz
#   bash /opt/studio/Server/deploy/upgrade.sh            # newest /root/studio-src*.tar.gz
#
# The tarball is a `git archive` of the repo (the same shape meta-lxc.sh pushes):
# the repo root at the top level, or one wrapper directory with --strip-components.
# Nothing outside /opt/studio is touched: the data (/srv/studio/data), the blob
# store (/data/blobs) and the browser mirrors (/srv/studio/web) are host paths in
# Server/docker-compose.yml, so they survive a source swap.
#
# Steps: extract -> verify -> keep the old checkout as /opt/studio.prev ->
# `docker compose up -d --build` -> wait for both health endpoints -> summary.
# On failure the previous checkout is named in the rollback hint; it is NOT
# restored automatically, because a rolled-back checkout may need a newer DB
# schema and the decision belongs to a person.
#
# Env (defaults): APP_DIR (/opt/studio) STUDIO_PORT (8770) WEB_PORT (8780)
#                 HEALTH_TIMEOUT (300 s) KEEP_PREV (1: keep /opt/studio.prev)
set -euo pipefail

log() { printf '[upgrade %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf '[upgrade] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
APP_DIR="${APP_DIR:-/opt/studio}"
STUDIO_PORT="${STUDIO_PORT:-8770}"
WEB_PORT="${WEB_PORT:-8780}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"

TARBALL="${1:-}"
if [ -z "$TARBALL" ]; then
  TARBALL="$(ls -t /root/studio-src*.tar.gz /root/studio-src*.tgz 2>/dev/null | head -n1 || true)"
fi
[ -n "$TARBALL" ] || die "no tarball given and none found in /root (studio-src*.tar.gz)"
[ -f "$TARBALL" ] || die "tarball not found: $TARBALL"

[ -d "$APP_DIR" ] || die "$APP_DIR does not exist: this is a fresh install, use install.sh"

WORK="$(mktemp -d /root/studio-upgrade.XXXXXX)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

log "extracting $TARBALL"
mkdir -p "$WORK/new"
tar -xzf "$TARBALL" -C "$WORK/new"
# a git archive of a tag has "<repo>-<ref>/" on top; a plain tar of the root has not
if [ ! -f "$WORK/new/Server/deploy/install.sh" ]; then
  inner="$(find "$WORK/new" -maxdepth 2 -name Server -type d | head -n1 || true)"
  [ -n "$inner" ] || die "the tarball has no Server/ directory"
  src="$(dirname "$inner")"
  log "tarball has a wrapper directory: using ${src#$WORK/new/}"
  mv "$src" "$WORK/root"
else
  mv "$WORK/new" "$WORK/root"
fi
for f in Server/docker-compose.yml Server/Dockerfile Server/studio_web/__init__.py \
         Server/studio_server/run.py Scripts/coding_web/browser_worker.py; do
  [ -f "$WORK/root/$f" ] || die "the tarball is not a CodingStudio checkout: $f is missing"
done

# the data directories must stay where compose expects them; a new compose file that
# points somewhere else would silently start from an empty DB, so say it out loud
grep -q '/srv/studio/data:/data' "$WORK/root/Server/docker-compose.yml" \
  || log "WARN the new compose file does not mount /srv/studio/data:/data - check it before the next step"

PREV="$APP_DIR.prev"
if [ "${KEEP_PREV:-1}" = "1" ]; then
  rm -rf "$PREV"
  log "keeping the current checkout as $PREV (rollback copy)"
  cp -a "$APP_DIR" "$PREV"
fi

log "swapping in the new checkout"
mv "$APP_DIR" "$APP_DIR.old.$$" 2>/dev/null || true
mv "$WORK/root" "$APP_DIR"
rm -rf "$APP_DIR.old.$$"

cd "$APP_DIR/Server"
PROFILE=()
# the bundled Caddy (compose profile "caddy") is a container, not a file: if it was
# running before the swap, keep it
if docker compose ps -q caddy 2>/dev/null | grep -q .; then PROFILE=(--profile caddy); fi
log "building and starting the stack (the first build after a change takes a few minutes)"
if ! docker compose ${PROFILE[@]+"${PROFILE[@]}"} up -d --build; then
  cat <<EOF >&2

[upgrade] the build or start failed.
  rollback: rm -rf $APP_DIR && mv $PREV $APP_DIR \\
            && cd $APP_DIR/Server && docker compose up -d --build
  logs:     cd $APP_DIR/Server && docker compose logs --tail 100
EOF
  exit 1
fi

wait_health() {
  local url="$1" what="$2" i
  for i in $(seq 1 $((HEALTH_TIMEOUT / 3))); do
    if curl -fsS --max-time 4 "$url" >/dev/null 2>&1; then
      log "$what: healthy"
      return 0
    fi
    sleep 3
  done
  return 1
}

if ! wait_health "http://127.0.0.1:$STUDIO_PORT/api/health" "sync server"; then
  docker compose logs --tail 80 studio || true
  cat <<EOF >&2

[upgrade] the sync server did not become healthy within ${HEALTH_TIMEOUT}s.
  rollback: rm -rf $APP_DIR && mv $PREV $APP_DIR \\
            && cd $APP_DIR/Server && docker compose up -d --build
EOF
  exit 1
fi
if ! wait_health "http://127.0.0.1:$WEB_PORT/app/health" "browser port"; then
  docker compose logs --tail 80 studio || true
  log "WARN the browser port is not healthy; the sync server is up and desktop clients keep working"
  log "     logs: cd $APP_DIR/Server && docker compose logs studio"
fi

# docker's own healthcheck needs a few intervals before it reports "healthy";
# wait for it so the health check below does not report a fresh start as FAIL.
log "waiting for the containers' docker health status"
for _ in $(seq 1 60); do
  pending=0
  for c in $(cd "$APP_DIR/Server" && docker compose ps -q); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null || echo none)"
    [ "$st" = "starting" ] && pending=1
  done
  [ "$pending" = "0" ] && break
  sleep 2
done
log "running the health check"
bash "$APP_DIR/Server/deploy/health-check.sh" || true

cat <<EOF

[upgrade] done.
  checkout:  $APP_DIR   (previous: $PREV)
  health:    bash $APP_DIR/Server/deploy/health-check.sh
  rollback:  rm -rf $APP_DIR && mv $PREV $APP_DIR && cd $APP_DIR/Server && docker compose up -d --build
EOF
