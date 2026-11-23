#!/usr/bin/env bash
# health-check.sh - status of the studio stack. Exits 1 when any check FAILs.
# WARN lines do not change the exit code.
#
#   bash /opt/studio/Server/deploy/health-check.sh
#   STUDIO_URL=http://127.0.0.1:8770 DISK_WARN_PCT=85 bash health-check.sh
#
# No `set -e`: every check runs and is reported.
set -uo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STUDIO_URL="${STUDIO_URL:-http://127.0.0.1:8770}"
DATA_DIR="${DATA_DIR:-/srv/studio/data}"
BLOB_DIR="${BLOB_DIR:-/data/blobs}"
BACKUP_DIR="${BACKUP_DIR:-/srv/studio/backups}"
DISK_WARN_PCT="${DISK_WARN_PCT:-85}"
DISK_FAIL_PCT="${DISK_FAIL_PCT:-95}"
BACKUP_MAX_AGE_H="${BACKUP_MAX_AGE_H:-26}"

FAILED=0
ok()   { echo "[health] OK   $*"; }
warn() { echo "[health] WARN $*"; }
fail() { echo "[health] FAIL $*"; FAILED=1; }

cd "$SERVER_DIR" || { fail "cannot enter $SERVER_DIR"; exit 1; }

# 1. API
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$STUDIO_URL/api/health" 2>/dev/null || echo 000)"
if [ "$code" = "200" ]; then ok "api /api/health -> 200"; else fail "api /api/health -> $code"; fi

# 2. container
cid="$(docker compose ps -q studio 2>/dev/null || true)"
if [ -z "$cid" ]; then
  fail "container: studio is not running"
else
  state="$(docker inspect -f '{{.State.Status}}' "$cid")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
  restarts="$(docker inspect -f '{{.RestartCount}}' "$cid")"
  if [ "$state" = "running" ] && [ "$health" = "healthy" ]; then
    ok "container: $state, $health (restarts: $restarts)"
  else
    fail "container: state=$state health=$health"
  fi
  mem="$(docker stats --no-stream --format '{{.MemUsage}} ({{.MemPerc}})' "$cid" 2>/dev/null || echo unknown)"
  ok "container memory: $mem"
fi

# 3. disk
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
if mountpoint -q "$BLOB_DIR"; then
  disk_check "$BLOB_DIR" "blobs"
else
  # df on an unmounted path reports the rootfs, so a missing mount must be a failure of its own
  fail "blobs: $BLOB_DIR is not a mount point (mp0 missing?)"
fi

# 4. backups
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
