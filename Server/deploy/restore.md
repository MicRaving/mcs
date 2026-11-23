# Restore and restore drill

What is backed up, where, and how to get it back.

| Data | Where it lives | Backed up by | Restored from |
|---|---|---|---|
| SQLite DB (users, projects, locks, changes, audit) | `/srv/studio/data/studio.db` (rootfs) | `backup.sh`, nightly 02:30 → `$BACKUP_DIR/studio-<UTC stamp>.db` (30-day retention) | newest good snapshot |
| Blobs (PDFs, datasets, content-addressed, immutable) | `/data/blobs` (mp0, separate extHDD volume, `backup=0`) | only if `BLOB_BACKUP_DIR` is set (rsync, new files only) and/or `RCLONE_REMOTE` (off-site, e.g. Nextcloud) | the mirror / the remote |
| The CT itself (OS, Docker, repo checkout, config) | `local-lvm` rootfs | Proxmox `vzdump` of CT 112 to `extBackups` (rootfs only) | `pct restore` |

Important: **the blob volume is not in `vzdump`** (`backup=0`, so the nightly CT backup
stays small). Blob protection therefore depends on `BLOB_BACKUP_DIR` and/or `RCLONE_REMOTE`.
If neither is configured, the blobs have no backup. Check this during the drill.

Snapshots and mirrors are only as good as the restore drill, so run section 5 before
relying on them.

Variables used below: `RESTORE` is a snapshot path, `BLOBS_BAK` is the blob mirror.

---

## 1. Restore only the database (the common case)

Use this after data corruption in `studio.db`, or to roll back a bad change. Blobs are not
touched.

```bash
cd /opt/studio/Server
docker compose stop studio

# pick the snapshot: list newest first and check it
ls -lt /srv/studio/backups/studio-*.db | head
RESTORE=/srv/studio/backups/studio-<stamp>.db
sqlite3 "$RESTORE" 'PRAGMA integrity_check;'          # must print: ok

# keep the damaged DB for forensics, then replace it
mkdir -p /srv/studio/forensics
mv /srv/studio/data/studio.db /srv/studio/forensics/studio-$(date -u +%Y%m%dT%H%M%SZ).db
rm -f /srv/studio/data/studio.db-wal /srv/studio/data/studio.db-shm
cp "$RESTORE" /srv/studio/data/studio.db
chown 10001:10001 /srv/studio/data/studio.db

docker compose up -d studio
bash /opt/studio/Server/deploy/health-check.sh
```

Changes made after the snapshot time are lost. Users who worked offline keep their local
copies. Their outbox pushes are checked against the restored server state (the change log
and conflicts make this visible, nothing is silently overwritten).

## 2. Restore blobs

Blobs are immutable and content-addressed, so a restore copies files back and never
overwrites a file that already exists.

```bash
cd /opt/studio/Server
docker compose stop studio
mountpoint -q /data/blobs || { echo "blob volume not mounted"; exit 1; }

# from the local mirror (BLOB_BACKUP_DIR)
rsync -a --ignore-existing "$BLOBS_BAK/" /data/blobs/
# or from off-site (RCLONE_REMOTE)
rclone copy "<remote>:<path>/blobs/" /data/blobs/ --ignore-existing --transfers 4

chown -R 10001:10001 /data/blobs
docker compose up -d studio
bash /opt/studio/Server/deploy/health-check.sh
```

Check that every blob the DB references exists. Blob hashes in the DB come from the
`blobs` table. A missing blob means the mirror is older than the DB snapshot. Re-run the
blob copy after a newer mirror run, or restore the DB from a snapshot taken before the
mirror's last run.

## 3. Restore the whole container (rootfs loss)

Use this when the CT itself is broken and the rootfs cannot be repaired. It needs a `vzdump`
archive of CT 112 on `extBackups`.

```bash
# on the Proxmox host
ls -lt /mnt/pve/extBackups/dump/vzdump-lxc-112-*.tar.zst | head
pct restore 112 /mnt/pve/extBackups/dump/vzdump-lxc-112-<stamp>.tar.zst --storage local-lvm
pct config 112        # check mp0: the blob volume is backup=0 and may be missing or empty
pct start 112
```

Then, inside CT 112:

```bash
mountpoint -q /data/blobs || echo "mp0 is missing: re-create it, see section 4"
bash /opt/studio/Server/deploy/health-check.sh
```

The DB is restored with the CT (last vzdump, usually last night). If `/srv/studio/backups`
is also gone, use section 1 with the newest copy from off-site (`$RCLONE_REMOTE/db/`).

Blob data comes only from the blob mirror or off-site, as in section 2.

## 4. Re-create the blob mount point (new host or lost volume)

```bash
# on the Proxmox host, CT stopped
pct stop 112
pct set 112 --mp0 extHDD:200,mp=/data/blobs,backup=0
pct start 112
pct exec 112 -- mountpoint -q /data/blobs && echo "mounted"
```

`pct set --mp0` creates a new 200 GB volume. It is empty. Then run section 2.

## 5. Restore drill (run before go-live and after changing backup settings)

The drill proves that a snapshot and the blobs restore into a working server, without
touching production data. Run it on CT 112 as the last step of an install, and once per
quarter.

```bash
set -euo pipefail
DRILL=/srv/studio/drill
rm -rf "$DRILL"; install -d -m 0750 "$DRILL/data" "$DRILL/blobs"

# a) snapshot: newest, integrity check, row counts against the live DB
SNAP="$(ls -t /srv/studio/backups/studio-*.db | head -n1)"
cp "$SNAP" "$DRILL/data/studio.db"
sqlite3 "$DRILL/data/studio.db" 'PRAGMA integrity_check;'            # must print: ok
for t in users projects; do
  echo "$t: drill=$(sqlite3 "$DRILL/data/studio.db" "select count(*) from $t") live=$(sqlite3 /srv/studio/data/studio.db "select count(*) from $t")"
done

# b) blobs: a sample is enough for the drill, the full set is checked by section 2
#    (copy a few hundred files; the DB references all of them)
#    -- or mirror everything if the store is small enough

# c) start a throw-away server on the drilled copy (port 8771, not the real one)
chown -R 10001:10001 "$DRILL"
docker run -d --rm --name studio-drill -p 127.0.0.1:8771:8770 \
  -v "$DRILL/data:/data" -v "$DRILL/blobs:/data/blobs" studio-server:local
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:8771/api/health >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:8771/api/health && echo " <- drill server healthy"
docker stop studio-drill

# d) clean up
rm -rf "$DRILL"
```

Pass criteria:

- `integrity_check` prints `ok`
- row counts match the live DB, or differ only by changes made after the snapshot
- the drill server answers `/api/health` with 200
- the restored blob sample opens: GET one known blob hash through the API (or confirm the
  file is present and non-empty)

Record the date, the snapshot name and the result in the project's ops log.

## 6. Checklist after any restore

- `bash /opt/studio/Server/deploy/health-check.sh` exits 0.
- A test login works with a non-admin account.
- A project opens and one document (`Codings/...`) loads.
- Next nightly backup runs: `tail /var/log/studio-backup.log`.
- Remove the forensics copy only after a week.
