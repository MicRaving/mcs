#!/usr/bin/env bash
# health-check.sh - status of the studio stack. Exits 1 when any check FAILs.
# WARN lines do not change the exit code.
#
#   bash /opt/studio/Server/deploy/health-check.sh
#   STUDIO_URL=http://127.0.0.1:8770 WEB_URL=http://127.0.0.1:8780 DISK_WARN_PCT=85 bash health-check.sh
#
# Two services (Server/docker-compose.yml): `studio` (the sync server, desktop
# clients) and `web` (the browser gateway and its per-user workers, PLAN_online_only
# §8).  The browser side is checked for: the gateway answers, the container is
# healthy, how many workers run and how much memory they hold (the gateway's own
# PSS sum), and the mirror volume's free space.
#
# No `set -e`: every check runs and is reported.
set -uo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STUDIO_URL="${STUDIO_URL:-http://127.0.0.1:8770}"
WEB_URL="${WEB_URL:-http://127.0.0.1:8780}"
DATA_DIR="${DATA_DIR:-/srv/studio/data}"
WEB_DIR="${WEB_DIR:-/srv/studio/web}"
BLOB_DIR="${BLOB_DIR:-/data/blobs}"
BACKUP_DIR="${BACKUP_DIR:-/srv/studio/backups}"
DISK_WARN_PCT="${DISK_WARN_PCT:-85}"
DISK_FAIL_PCT="${DISK_FAIL_PCT:-95}"
BACKUP_MAX_AGE_H="${BACKUP_MAX_AGE_H:-26}"
# the gateway's pressure ceiling (compose: STUDIO_WEB_PRESSURE_MB); at or above it
# the supervisor evicts idle workers, so a sustained reading there is a warning
WEB_PRESSURE_MB="${WEB_PRESSURE_MB:-1450}"

FAILED=0
ok()   { echo "[health] OK   $*"; }
warn() { echo "[health] WARN $*"; }
fail() { echo "[health] FAIL $*"; FAILED=1; }

cd "$SERVER_DIR" || { fail "cannot enter $SERVER_DIR"; exit 1; }

# 1. sync server API
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$STUDIO_URL/api/health" 2>/dev/null)"
code="${code:-000}"
if [ "$code" = "200" ]; then ok "api /api/health -> 200"; else fail "api /api/health -> $code"; fi

# 2. containers
container_check() {
  local svc="$1" want_health="$2"
  local cid state health restarts mem
  cid="$(docker compose ps -q "$svc" 2>/dev/null || true)"
  if [ -z "$cid" ]; then
    fail "container: $svc is not running"
    return
  fi
  state="$(docker inspect -f '{{.State.Status}}' "$cid")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"
  if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || { [ "$want_health" = "any" ] && [ "$health" != "unhealthy" ]; }; }; then
    ok "container: $svc $state, $health (restarts: $restarts)"
  else
    fail "container: $svc state=$state health=$health"
  fi
  mem="$(docker stats --no-stream --format '{{.MemUsage}} ({{.MemPerc}})' "$cid" 2>/dev/null || echo unknown)"
  ok "container memory: $svc $mem"
}
container_check studio yes

# 3. browser gateway
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$WEB_URL/app/health" 2>/dev/null)"
code="${code:-000}"
if [ "$code" = "200" ]; then ok "gateway /app/health -> 200"; else fail "gateway /app/health -> $code"; fi
container_check web yes

# the gateway's own view: worker count, summed PSS, heavy lane, launcher and the
# last lifecycle events.  /app/health hides the detail from non-loopback clients, so
# ask it from inside the container (a published port would look remote), and format
# there too: the CT host has no python of its own.
detail="$(docker compose exec -T web python - "$WEB_PRESSURE_MB" <<'PY' 2>/dev/null
import json, sys, urllib.request

pressure = float(sys.argv[1])
d = json.loads(urllib.request.urlopen("http://127.0.0.1:8780/app/health", timeout=5).read() or b"{}")
rss = float(d.get("rss_mb") or 0)
lane = d.get("lane") or {}
print(f"[health] OK   gateway workers: {d.get('workers')} running, {rss:.0f} MB PSS "
      f"(launcher {d.get('launcher')}, heavy lane {lane.get('waiting', 0)} waiting)")
if rss >= pressure:
    print(f"[health] WARN gateway workers at {rss:.0f} MB, pressure ceiling is {pressure:.0f} MB")
for ev in (d.get("events") or [])[-5:]:
    print(f"[health]      last event: {ev.get('event')} user={ev.get('user')} "
          f"reason={ev.get('reason', '')} rss_mb={ev.get('rss_mb', '')}")
PY
)"
if [ -z "$detail" ]; then
  warn "gateway detail: not readable from inside the web container"
else
  printf '%s\n' "$detail"
fi

# 3b. the workers read the blob store through symlinks: uid 10001 must be able to
# open a blob, or every paper, dataset and coding file fails (a blob store written
# by a root server is the classic case; install.sh fixes ownership)
WEB_BLOB_MOUNT="${WEB_BLOB_MOUNT:-/blobs}"
if mountpoint -q "$BLOB_DIR"; then
  sample="$(find "$BLOB_DIR" -maxdepth 2 -type f -print -quit 2>/dev/null)"
  if [ -z "$sample" ]; then
    warn "blobs: no file under $BLOB_DIR to test readability"
  else
    rel="${sample#"$BLOB_DIR"/}"
    if docker compose exec -T web python -c 'import sys; open(sys.argv[1], "rb").read(1)' \
         "$WEB_BLOB_MOUNT/$rel" >/dev/null 2>&1; then
      ok "blobs: readable by the browser worker ($rel)"
    else
      fail "blobs: $rel is not readable by the browser worker (chown $BLOB_DIR to 10001)"
    fi
  fi
fi

# 4. disk
disk_check() {
  local path="$1" label="$2" pct avail
  if [ ! -d "$path" ]; then
    fail "$label: $path does not exist"
    return
  fi
  pct="$(df -P "$path" | awk 'NR==2 {gsub("%", "", $5); print $5}')"
  avail="$(df -Ph "$path" | awk 'NR==2 {print $4}')"
  if [ "$pct" -ge "$DISK_FAIL_PCT" ]; then
    fail "$label: ${pct}% used, ${avail} free ($path)"
  elif [ "$pct" -ge "$DISK_WARN_PCT" ]; then
    warn "$label: ${pct}% used, ${avail} free ($path)"
  else
    ok "$label: ${pct}% used, ${avail} free ($path)"
  fi
}
disk_check "$DATA_DIR" "data"
disk_check "$WEB_DIR" "mirrors"
if mountpoint -q "$BLOB_DIR"; then
  disk_check "$BLOB_DIR" "blobs"
else
  # df on an unmounted path reports the rootfs, so a missing mount must be a failure of its own
  fail "blobs: $BLOB_DIR is not a mount point (mp0 missing?)"
fi
if mountpoint -q "$WEB_DIR"; then
  ok "mirrors: $WEB_DIR is its own volume (mp1)"
else
  # an install from before browser mode has no mp1; the mirrors then share the rootfs
  warn "mirrors: $WEB_DIR is on the rootfs (mp1 missing, PLAN_online_only §8)"
fi

# 5. backups
recent="$(find "$BACKUP_DIR" -maxdepth 1 -name 'studio-*.db' -mmin "-$((BACKUP_MAX_AGE_H * 60))" 2>/dev/null | head -n1)"
if [ -n "$recent" ]; then
  ok "backup: recent snapshot $(basename "$recent")"
else
  warn "backup: no snapshot younger than ${BACKUP_MAX_AGE_H} h in $BACKUP_DIR"
fi

if [ "$FAILED" = "1" ]; then
  echo "[health] FAILED"
  exit 1
fi
echo "[health] all checks passed"
