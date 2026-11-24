#!/usr/bin/env bash
# backup.sh - nightly backup of the studio stack (cron 02:30, installed by install.sh).
#
#   1. SQLite online backup of studio.db -> $BACKUP_DIR/studio-<stamp>.db, integrity-checked.
#      Small; always made.
#   2. Browser mirrors (only the unpushed state): the gateway is asked to drain every
#      worker (POST /app/admin/drain, loopback inside the container), then the
#      `<mirror>/.studio/` directories - sync state, outbox, queued versions - are
#      tarred to $BACKUP_DIR/web-state-<stamp>.tar.gz.  The mirrors themselves are
#      rebuildable from the server (symlinks into the blob store + downloaded files)
#      and are deliberately NOT backed up: that volume is thousands of files of
#      derived data (PLAN_online_only §8).
#   3. Blobs (optional): if BLOB_BACKUP_DIR is set, rsync copies NEW files only. Blobs are
#      immutable and content-addressed, so an existing file is never rewritten
#      (--ignore-existing). Nothing is deleted from the mirror.
#   4. Off-site (optional): if RCLONE_REMOTE is set (e.g. a Nextcloud WebDAV remote made with
#      `rclone config`), the new DB snapshot and the new web state go to
#      $RCLONE_REMOTE/db/ and $RCLONE_REMOTE/web/, new blobs to $RCLONE_REMOTE/blobs/.
#   5. Retention: DB snapshots and web state older than RETENTION_DAYS (default 30) are
#      deleted, locally and remotely. Blob copies are never pruned automatically.
#
# Blobs are not copied to the rootfs by default: the blob volume is a separate mount point
# (extHDD) and the rootfs is small. The Proxmox vzdump of the CT covers the rootfs only.
#
# Env (defaults): DATA_DIR (/srv/studio/data) WEB_DIR (/srv/studio/web) BLOB_DIR
# (/data/blobs) BACKUP_DIR (/srv/studio/backups) BLOB_BACKUP_DIR (unset) RCLONE_REMOTE
# (unset) RETENTION_DAYS (30) SERVER_DIR (derived from this file's location).
#
# Run as: bash backup.sh   (cron calls it that way; no exec bit needed)
set -euo pipefail
umask 077

SERVER_DIR="${SERVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${DATA_DIR:-/srv/studio/data}"
WEB_DIR="${WEB_DIR:-/srv/studio/web}"
BLOB_DIR="${BLOB_DIR:-/data/blobs}"
BACKUP_DIR="${BACKUP_DIR:-/srv/studio/backups}"
BLOB_BACKUP_DIR="${BLOB_BACKUP_DIR:-}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

DB="$DATA_DIR/studio.db"
LOCK=/run/studio-backup.lock
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_DIR/studio-$STAMP.db"
WEB_DEST="$BACKUP_DIR/web-state-$STAMP.tar.gz"

log() { printf '[backup %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '[backup %s] WARN %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { printf '[backup %s] ERROR: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

exec 9>"$LOCK"
flock -n 9 || die "another backup is already running ($LOCK)"

[ -f "$DB" ] || die "no database at $DB"
mkdir -p "$BACKUP_DIR"

if [ -n "$BLOB_BACKUP_DIR" ] || [ -n "$RCLONE_REMOTE" ]; then
  mountpoint -q "$BLOB_DIR" || die "$BLOB_DIR is not mounted; refusing to mirror an empty store"
fi

# ---- 1. SQLite snapshot -----------------------------------------------------
if command -v sqlite3 >/dev/null 2>&1; then
  log "sqlite3 online backup -> $DEST"
  sqlite3 "$DB" ".timeout 10000" ".backup '$DEST'"
  check="$(sqlite3 "$DEST" 'PRAGMA integrity_check;')"
  candidate="$DEST"
else
  # fallback without the sqlite3 CLI: the container's python writes into the shared data dir
  TMP_NAME=".backup-$STAMP.db"
  log "sqlite3 CLI absent; backing up through the container's python"
  (cd "$SERVER_DIR" && docker compose exec -T studio python -c \
    "import sqlite3,sys; s=sqlite3.connect('/data/studio.db', timeout=10); d=sqlite3.connect('/data/'+sys.argv[1]); s.backup(d); d.close(); s.close()" \
    "$TMP_NAME")
  check="$(cd "$SERVER_DIR" && docker compose exec -T studio python -c \
    "import sqlite3,sys; print(sqlite3.connect('/data/'+sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" \
    "$TMP_NAME")"
  candidate="$DATA_DIR/$TMP_NAME"
fi

if [ "$check" != "ok" ]; then
  rm -f "$candidate"
  die "integrity_check failed for the snapshot: $check"
fi
if [ "$candidate" != "$DEST" ]; then
  mv "$candidate" "$DEST"
fi
chmod 600 "$DEST"
log "snapshot ok: $(basename "$DEST") ($(du -h "$DEST" | cut -f1))"

# ---- 2. browser mirrors: only the unpushed state ------------------------------
if [ -d "$WEB_DIR" ]; then
  if [ -n "$(cd "$SERVER_DIR" && docker compose ps -q web 2>/dev/null || true)" ]; then
    log "draining browser workers (POST /app/admin/drain, loopback inside the container)"
    if ! (cd "$SERVER_DIR" && docker compose exec -T web python -c '
import json, os, urllib.request
# the gateway is loopback-only here and requires the site origin on a non-GET
# request: the container knows its own public origin
origin = os.environ.get("STUDIO_WEB_PUBLIC_ORIGIN") or "http://127.0.0.1:8780"
req = urllib.request.Request("http://127.0.0.1:8780/app/admin/drain", method="POST",
                             headers={"Origin": origin})
out = json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")
print("drain ok" if out.get("ok") else "drain reported a pending outbox: " + json.dumps(out)[:400])
'); then
      warn "the gateway did not confirm the drain; the outboxes are backed up as they are"
    fi
  else
    warn "the web container is not running; the outboxes are backed up as they are"
  fi
  if [ -n "$(find "$WEB_DIR" -type d -name .studio -print -quit 2>/dev/null)" ]; then
    log "browser sync state + outboxes -> $WEB_DEST"
    # relative paths of every <mirror>/.studio, tarred from WEB_DIR; the mirrors
    # themselves are not included (rebuildable, large).  Idle workers keep polling,
    # so a state file may change while it is read: tar says so and exits 1, which is
    # not a failed backup (the outbox is append-only, the state is derived).
    find "$WEB_DIR" -type d -name .studio -printf '%P\0' \
      | tar -czf "$WEB_DEST" -C "$WEB_DIR" --null -T - --warning=no-file-changed || rc=$?
    rc=${rc:-0}
    if [ "$rc" -gt 1 ]; then
      die "tar failed with exit $rc"
    elif [ "$rc" = "1" ]; then
      warn "a mirror state file changed while it was tarred (a worker was syncing); the outbox is complete"
    fi
    chmod 600 "$WEB_DEST"
    log "web state ok: $(basename "$WEB_DEST") ($(du -h "$WEB_DEST" | cut -f1))"
  else
    log "no browser mirror state under $WEB_DIR (nobody logged in yet); nothing to keep"
  fi
else
  warn "no browser mirror volume at $WEB_DIR"
fi

# ---- 3. blob mirror (new files only) ----------------------------------------
if [ -n "$BLOB_BACKUP_DIR" ]; then
  command -v rsync >/dev/null 2>&1 || die "BLOB_BACKUP_DIR is set but rsync is not installed"
  mkdir -p "$BLOB_BACKUP_DIR"
  log "blob mirror (new files only) -> $BLOB_BACKUP_DIR"
  rsync -a --ignore-existing "$BLOB_DIR/" "$BLOB_BACKUP_DIR/"
fi

# ---- 4. off-site -------------------------------------------------------------
if [ -n "$RCLONE_REMOTE" ]; then
  command -v rclone >/dev/null 2>&1 || die "RCLONE_REMOTE is set but rclone is not installed"
  log "off-site: DB snapshot -> $RCLONE_REMOTE/db/"
  rclone copy "$DEST" "$RCLONE_REMOTE/db/"
  if [ -f "$WEB_DEST" ]; then
    log "off-site: browser sync state -> $RCLONE_REMOTE/web/"
    rclone copy "$WEB_DEST" "$RCLONE_REMOTE/web/"
  fi
  log "off-site: new blobs -> $RCLONE_REMOTE/blobs/"
  rclone copy "$BLOB_DIR" "$RCLONE_REMOTE/blobs/" --ignore-existing --transfers 4
  log "off-site: retention ${RETENTION_DAYS} days on $RCLONE_REMOTE/db/ and /web/"
  rclone delete "$RCLONE_REMOTE/db/" --min-age "${RETENTION_DAYS}d"
  rclone delete "$RCLONE_REMOTE/web/" --min-age "${RETENTION_DAYS}d"
fi

# ---- 5. local retention -----------------------------------------------------
log "local retention: snapshots and web state older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'studio-*.db' -mtime "+$RETENTION_DAYS" -print -delete
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'web-state-*.tar.gz' -mtime "+$RETENTION_DAYS" -print -delete

log "done"
