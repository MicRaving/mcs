#!/usr/bin/env bash
# pve-oci-backup.sh - nightly backup for a studio run as a Proxmox OCI application
# container (pve-oci.sh).  Runs on the PROXMOX HOST: the container has no cron and
# no shell tools, but its data directories are host directories (bind mounts).
#
#   1. SQLite online backup of <data>/studio.db (python3's sqlite3 backup API, safe
#      while the server runs in WAL mode) -> $BACKUP_DIR/studio-<stamp>.db,
#      integrity-checked; a failed check deletes the copy and exits non-zero.
#   2. The browser mirrors' unpushed state (<web>/**/.studio/) -> web-state-<stamp>.tar.gz
#      (the mirrors themselves are rebuildable from the server and not copied).
#   3. Retention: snapshots older than RETENTION_DAYS (30) are deleted.
#   Blobs are immutable and live on their own disk; back them up separately.
#
# Install (cron 02:30):
#   install -m 0644 /dev/stdin /etc/cron.d/studio-oci-backup <<'EOF'
#   30 2 * * * root bash /root/studio-oci/pve-oci-backup.sh >> /var/log/studio-oci-backup.log 2>&1
#   EOF
#
# Env (defaults): DATA_DIR (/srv/studio-oci/data) WEB_DIR (/srv/studio-oci/web)
# BACKUP_DIR (/srv/studio-oci/backups) RETENTION_DAYS (30)
set -euo pipefail
umask 077

DATA_DIR="${DATA_DIR:-/srv/studio-oci/data}"
WEB_DIR="${WEB_DIR:-/srv/studio-oci/web}"
BACKUP_DIR="${BACKUP_DIR:-/srv/studio-oci/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
log() { printf '[studio-backup %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

exec 9>/run/studio-oci-backup.lock
flock -n 9 || { log "another backup is running"; exit 1; }
[ -f "$DATA_DIR/studio.db" ] || { log "ERROR no database at $DATA_DIR/studio.db"; exit 1; }
mkdir -p "$BACKUP_DIR"

DEST="$BACKUP_DIR/studio-$STAMP.db"
python3 - "$DATA_DIR/studio.db" "$DEST" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close(); src.close()
if ok != "ok":
    import os; os.unlink(sys.argv[2]); sys.exit(f"integrity_check: {ok}")
PY
log "database -> $DEST ($(du -h "$DEST" | cut -f1))"

if [ -d "$WEB_DIR" ]; then
  mapfile -t states < <(cd "$WEB_DIR" && find . -type d -name .studio -prune -print 2>/dev/null)
  if [ "${#states[@]}" -gt 0 ]; then
    tar -C "$WEB_DIR" -czf "$BACKUP_DIR/web-state-$STAMP.tar.gz" "${states[@]}"
    log "browser state -> web-state-$STAMP.tar.gz (${#states[@]} mirrors)"
  fi
fi

find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'studio-*.db' -o -name 'web-state-*.tar.gz' \) \
  -mtime +"$RETENTION_DAYS" -print -delete | sed 's/^/[studio-backup] pruned /'
log "done"
