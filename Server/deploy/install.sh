#!/usr/bin/env bash
# install.sh - runs INSIDE the studio CT (called by meta-lxc.sh, or by hand as root).
# Idempotent: re-running rebuilds and restarts the stack; data and the admin account are kept.
#
#   packages -> Docker -> data dirs -> compose up --build -> health wait
#   -> first admin (once) -> nightly backup cron (02:30)
#
# The stack is two services (Server/docker-compose.yml): `studio` (the sync server,
# 8770, desktop clients) and `web` (the browser gateway + per-user workers, 8780;
# PLAN_online_only §8).  Both are waited for; a broken browser side does not take
# the sync server down, but the install fails loudly and names it.
#
# Optional settings come from /root/.studio-install.env (written by meta-lxc.sh with mode 600;
# removed after it is read): ADMIN_USER, ADMIN_PASSWORD, DATA_DIR, WEB_DIR, BACKUP_DIR,
# BLOB_BACKUP_DIR, RCLONE_REMOTE, STUDIO_CADDY, STUDIO_DOMAIN.
set -euo pipefail

log() { printf '[install %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"

HANDOFF=/root/.studio-install.env
if [ -f "$HANDOFF" ]; then
  set -a
  . "$HANDOFF"
  set +a
  rm -f "$HANDOFF"
  log "settings read from the handoff file (now removed)"
fi

DATA_DIR="${DATA_DIR:-/srv/studio/data}"
WEB_DIR="${WEB_DIR:-/srv/studio/web}"
BACKUP_DIR="${BACKUP_DIR:-/srv/studio/backups}"
BLOB_BACKUP_DIR="${BLOB_BACKUP_DIR:-}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
STATE_DIR=/srv/studio/state
BLOB_DIR=/data/blobs
STUDIO_PORT=8770
WEB_PORT=8780
CONTAINER_UID=10001
ADMIN_USER="${ADMIN_USER:-admin}"
STUDIO_CADDY="${STUDIO_CADDY:-0}"
export STUDIO_DOMAIN="${STUDIO_DOMAIN:-meta.home.mfendt.de}"

# ---- 1. packages ------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
PKGS="ca-certificates curl openssl sqlite3 rsync cron util-linux"
if [ -n "$RCLONE_REMOTE" ]; then PKGS="$PKGS rclone"; fi
apt-get update -y >/dev/null
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS >/dev/null
systemctl enable --now cron >/dev/null 2>&1 || true

# ---- 2. Docker ----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker (get.docker.com)"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
systemctl enable --now docker >/dev/null 2>&1 || true
log "docker: $(docker --version)"

# ---- 3. storage ---------------------------------------------------------------
mountpoint -q "$BLOB_DIR" || die "$BLOB_DIR is not a mount point: check mp0 in /etc/pve/lxc/<CTID>.conf"
install -d -m 0750 "$DATA_DIR" "$BACKUP_DIR" "$STATE_DIR"
# the browser side's mirrors + per-user caches (local-lvm; rebuildable, so not in
# the vzdump and not in backup.sh's data set)
install -d -m 0750 "$WEB_DIR"
if ! mountpoint -q "$WEB_DIR"; then
  # an existing CT installed before browser mode has no mp1: it still works on the
  # rootfs, but the mirrors then share it with the images (PLAN_online_only §8)
  log "WARN $WEB_DIR is not a mount point (mp1 missing): mirrors will live on the rootfs"
fi
# the container runs as UID $CONTAINER_UID; fix only entries with a wrong owner (idempotent)
find "$DATA_DIR" ! -user "$CONTAINER_UID" -exec chown "$CONTAINER_UID:$CONTAINER_UID" {} +
find "$WEB_DIR" ! -user "$CONTAINER_UID" -exec chown "$CONTAINER_UID:$CONTAINER_UID" {} +
log "fixing ownership on $BLOB_DIR (only entries with a wrong owner)"
find "$BLOB_DIR" ! -user "$CONTAINER_UID" -exec chown "$CONTAINER_UID:$CONTAINER_UID" {} +

# ---- 4. stack -----------------------------------------------------------------
cd "$SERVER_DIR"
PROFILE=()
if [ "$STUDIO_CADDY" = "1" ]; then PROFILE=(--profile caddy); fi
log "building and starting the stack (the first build takes a few minutes)"
docker compose ${PROFILE[@]+"${PROFILE[@]}"} up -d --build

log "waiting for http://127.0.0.1:$STUDIO_PORT/api/health"
healthy=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 4 "http://127.0.0.1:$STUDIO_PORT/api/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [ "$healthy" != "1" ]; then
  docker compose logs --tail 80 studio || true
  die "studio did not become healthy within 120 s"
fi
log "studio is healthy"

log "waiting for http://127.0.0.1:$WEB_PORT/app/health (browser gateway)"
web_healthy=0
for _ in $(seq 1 90); do
  if curl -fsS --max-time 4 "http://127.0.0.1:$WEB_PORT/app/health" >/dev/null 2>&1; then
    web_healthy=1
    break
  fi
  sleep 2
done
if [ "$web_healthy" != "1" ]; then
  docker compose logs --tail 80 web || true
  die "studio-web did not become healthy within 180 s (the sync server on $STUDIO_PORT is up and keeps serving desktop clients)"
fi
log "studio-web is healthy"

# ---- 5. first admin (once) ----------------------------------------------------
ADMIN_MARK="$STATE_DIR/admin-created"
if [ -e "$ADMIN_MARK" ]; then
  log "first admin already created ($ADMIN_MARK); accounts untouched"
else
  GENERATED=0
  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    ADMIN_PASSWORD="$(openssl rand -hex 12)"
    GENERATED=1
  fi
  export ADMIN_USER ADMIN_PASSWORD
  log "creating admin '$ADMIN_USER'"
  docker compose exec -T -e ADMIN_USER -e ADMIN_PASSWORD studio python -m studio_server create-admin
  date -u +%Y-%m-%dT%H:%M:%SZ > "$ADMIN_MARK"
  if [ "$GENERATED" = "1" ]; then
    printf '\n  ==> admin login: %s\n  ==> password:    %s\n  ==> shown ONCE. It is not stored on this host; save it now.\n\n' \
      "$ADMIN_USER" "$ADMIN_PASSWORD"
  fi
  unset ADMIN_PASSWORD
fi

# ---- 6. nightly backup cron ---------------------------------------------------
CRON_FILE=/etc/cron.d/studio-backup
tmp="$(mktemp)"
{
  echo "SHELL=/bin/bash"
  echo "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  echo "DATA_DIR=$DATA_DIR"
  echo "WEB_DIR=$WEB_DIR"
  echo "BACKUP_DIR=$BACKUP_DIR"
  if [ -n "$BLOB_BACKUP_DIR" ]; then echo "BLOB_BACKUP_DIR=$BLOB_BACKUP_DIR"; fi
  if [ -n "$RCLONE_REMOTE" ]; then echo "RCLONE_REMOTE=$RCLONE_REMOTE"; fi
  echo "30 2 * * * root bash $DEPLOY_DIR/backup.sh >> /var/log/studio-backup.log 2>&1"
} > "$tmp"
install -m 0644 "$tmp" "$CRON_FILE"
rm -f "$tmp"
log "nightly backup installed: $CRON_FILE"

# ---- summary ------------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ "$STUDIO_CADDY" = "1" ]; then
  PROXY="bundled Caddy, https://$STUDIO_DOMAIN  (browser: https://$STUDIO_DOMAIN/app/)"
else
  PROXY="external proxy (see deploy/caddy-snippet.txt and deploy/caddy-browser-mode.txt)"
fi
cat <<EOF

======================================================
 studio server installed
======================================================
 local health:  http://127.0.0.1:$STUDIO_PORT/api/health      (sync server)
                http://127.0.0.1:$WEB_PORT/app/health       (browser gateway)
 LAN:           http://${IP:-?}:$STUDIO_PORT  (firewall 8770 to the proxy host only)
                http://${IP:-?}:$WEB_PORT   (firewall 8780 to the proxy host only)
 browser mode:  https://$STUDIO_DOMAIN/app/  (same accounts as the desktop)
 mirrors:       $WEB_DIR (rebuildable; excluded from backups except unpushed outboxes)
 proxy:         $PROXY
 status:        bash $DEPLOY_DIR/health-check.sh
 backups:       $BACKUP_DIR (02:30 nightly), log /var/log/studio-backup.log
 restore:       $DEPLOY_DIR/restore.md
 upgrade:       bash $DEPLOY_DIR/upgrade.sh /root/studio-src.tar.gz
======================================================
EOF
