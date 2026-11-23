# Studio server

The server side of CodingStudio's online projects. It is a small FastAPI application
(one process, one worker, SQLite in WAL mode) that stores the project's
accounts, members, sync change log, blobs (PDFs, datasets), locks, presence, assignments
and audit trail. Desktop clients and, later, the browser client talk to it over HTTPS.

Requirements for the target machine: 2 GB RAM is the budget (the container is capped at
1 GB), a small rootfs, and a separate large volume for blobs.

## Layout

```
Server/
  studio_server/        the FastAPI application (`python -m studio_server`)
  tests/                pytest suite (not shipped in the image)
  requirements.txt      runtime + dev dependencies
  requirements-prod.txt runtime dependencies only (used by the Dockerfile)
  Dockerfile            image, built from the repo root
  Dockerfile.dockerignore  allow-list for the build context
  docker-compose.yml    stack: studio (+ optional bundled caddy profile)
  deploy/
    meta-lxc.sh         Proxmox host: creates the LXC CT and installs the stack in it
    install.sh          inside the CT: Docker, data dirs, stack, first admin, backup cron
    health-check.sh     status check (exit 1 on failure)
    backup.sh           nightly SQLite snapshot, blob mirror, off-site, retention
    restore.md          restore runbook and restore drill
    caddy-snippet.txt   site block for the existing proxy on CT 110
    Caddyfile.bundled   proxy for installs without an existing proxy
```

The server imports `online_shared` (`Scripts/online_shared/`, stdlib only, shared with the
desktop client). In development this is found by adding `<repo>/Scripts` to `sys.path`
(see `studio_server/__init__.py`). In the image it is copied to `/app/online_shared`.

## Local development

Run from `Server/`. Use a scratch data directory outside the repo's data folders:

```bash
cd Server
python -m pip install -r requirements.txt
python -m studio_server create-admin          # reads ADMIN_USER / ADMIN_PASSWORD from the environment
python -m studio_server serve --host 127.0.0.1 --port 8770 --data ../temp/online/server-dev
curl http://127.0.0.1:8770/api/health
```

Tests:

```bash
cd Server
PYTHONPYCACHEPREFIX=../temp/pycache python -m pytest tests
```

Keep scratch data under `temp/` (the project's rule for intermediates).

## Docker

BuildKit is required. It is the default in current Docker (`DOCKER_BUILDKIT=1` only
matters on old versions). `Server/Dockerfile.dockerignore` is an allow-list and BuildKit
reads it because it sits next to the Dockerfile. Without BuildKit the whole repo, including
the large data folders, would be sent as the build context.

```bash
# from the repo root
docker build -f Server/Dockerfile -t studio-server:local .

# from Server/: studio only (an external proxy handles TLS)
docker compose up -d --build

# from Server/: studio + bundled Caddy (no external proxy)
STUDIO_DOMAIN=meta.example.org docker compose --profile caddy up -d
```

The container runs as UID 10001 (non-root, no login shell), with `no-new-privileges`, a
1 GB memory cap, and logs rotated at 10 MB × 3. The host directory `/srv/studio/data` must be
owned by 10001 (install.sh sets this). Data lives at `/data` inside the container, blobs at
`/data/blobs`.

Port 8770 is published on the host. The stack assumes the firewall allows it only from the
proxy host, because the server trusts `X-Forwarded-For` (`STUDIO_TRUST_PROXY=1`). With the
bundled Caddy profile, remove the `ports:` entry for `studio` once the proxy is in place.

## Proxmox install (production host)

Target: Proxmox VE 9.2, CT 112 `meta`, unprivileged Debian 12 with `nesting=1,keyctl=1`,
4 cores, 2 GB RAM, 512 MB swap, 16 GB rootfs on `local-lvm`, blob volume 200 GB on
`extHDD` at `/data/blobs` (`backup=0`), IP `192.168.178.12/24`.

On the Proxmox host, as root:

```bash
# check first (prints the pct commands and changes nothing)
DRY_RUN=1 REPO_SOURCE=https://github.com/<owner>/<repo>.git bash Server/deploy/meta-lxc.sh

# then for real; REPO_TOKEN only for a private repo (fine-grained PAT, Contents: read)
REPO_SOURCE=https://github.com/<owner>/<repo>.git REPO_TOKEN=<token> \
  bash Server/deploy/meta-lxc.sh
```

`meta-lxc.sh` refuses to run if CT 112 exists or the IP already answers ping. The script
creates the CT, mounts the blob volume, checks out the repo into `/opt/studio`, and runs
`install.sh`. `install.sh` installs Docker from get.docker.com, builds and starts the stack,
waits for `/api/health`, creates the first admin (once), and installs the nightly backup
cron.

The first admin's password is printed once if you did not set `ADMIN_PASSWORD`. It is not
stored on the host. Save it when it is shown.

Then:

1. Paste `Server/deploy/caddy-snippet.txt` into the Caddyfile on CT 110 and reload Caddy.
2. Make sure `meta.home.mfendt.de` resolves on the LAN (wildcard, or an A record on CT 106).
3. Firewall TCP 8770 on CT 112 so only 192.168.178.10 can reach it.
4. Check: `pct exec 112 -- bash /opt/studio/Server/deploy/health-check.sh`.
5. Log in at `https://meta.home.mfendt.de` from the LAN. Outside the LAN the request gets
   no response.

Docker in an unprivileged LXC sometimes fails with AppArmor errors. If that happens, add
`lxc.apparmor.profile: unconfined` to `/etc/pve/lxc/112.conf`, restart the CT, and note the
reason in the ops log.

### Settings

`meta-lxc.sh` reads these environment variables (defaults in brackets):

| variable | meaning |
|---|---|
| `CTID` `CT_HOSTNAME` | container id [112], hostname [meta] |
| `CORES` `RAM_MB` `SWAP_MB` `DISK` | [4] [2048] [512] [16G] |
| `STORAGE` `BLOB_STORAGE` `BLOB_SIZE` | rootfs storage [local-lvm], blob storage [extHDD], [200G] |
| `BRIDGE` `IP` `GW` | [vmbr0] [192.168.178.12/24] [192.168.178.1] |
| `TEMPLATE` | [local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst] |
| `REPO_SOURCE` `REPO_REF` `REPO_TOKEN` | repo (required), branch/tag, token for a private repo |
| `ADMIN_USER` `ADMIN_PASSWORD` | first admin [admin]; password is generated and printed once if unset |
| `BACKUP_DIR` | DB snapshots [/srv/studio/backups] (on the rootfs; small) |
| `BLOB_BACKUP_DIR` | blob mirror, rsync of new files only [unset = no mirror] |
| `RCLONE_REMOTE` | off-site, e.g. `nextcloud:studio` [unset] |
| `STUDIO_CADDY` `STUDIO_DOMAIN` | bundled Caddy profile [0], its public name |
| `DRY_RUN` | 1 = print the pct commands only |

The settings `ADMIN_PASSWORD`, `REPO_TOKEN` and the other variables reach the container in
a 600-mode file that `install.sh` deletes after reading. They are never placed on a command line.

## Operations

| task | command (inside CT 112) |
|---|---|
| status | `bash /opt/studio/Server/deploy/health-check.sh` (exit 1 on failure) |
| logs | `cd /opt/studio/Server && docker compose logs -f studio` |
| backup now | `bash /opt/studio/Server/deploy/backup.sh` |
| backup log | `tail -n 50 /var/log/studio-backup.log` |
| restore | `Server/deploy/restore.md` (DB only, blobs, whole CT, drill) |
| upgrade | `cd /opt/studio && git pull && cd Server && docker compose up -d --build` then health-check |
| admin password reset | `docker compose exec studio python -m studio_server reset-password` |

Upgrade notes: the schema is migrated on start (`PRAGMA user_version`). Take a backup
before an upgrade that changes the schema. `install.sh` is also safe to re-run.

### Backups

- **DB snapshots**: nightly 02:30 by cron (`/etc/cron.d/studio-backup`). Each snapshot is an
  online SQLite backup, checked with `PRAGMA integrity_check`. Kept 30 days in
  `BACKUP_DIR`.
- **Blobs**: the blob volume is not in the Proxmox `vzdump` (`backup=0`). Protection comes
  from `BLOB_BACKUP_DIR` (local mirror, new files only) and/or `RCLONE_REMOTE` (off-site,
  new files only). **If neither is set, blobs have no backup.** Set one of them.
- **Proxmox**: `vzdump` of CT 112 to `extBackups` covers the rootfs (OS, Docker, repo,
  config, DB as of the last run). Use it for the whole-CT restore.
- **Sizing**: `BACKUP_DIR` lives on the 16 GB rootfs. It holds DB snapshots only. Do not
  point `BLOB_BACKUP_DIR` at the rootfs; use a mount on `extBackups` (add a second mount
  point on the CT first).
- **Off-site**: `rclone config` as root creates the remote (type `webdav`, vendor
  `nextcloud`, the university Nextcloud URL and an app password). Set `RCLONE_REMOTE` to
  `<remote>:<folder>`. Do the first sync during a quiet time, because the blob set is large.
- Restore drill: `Server/deploy/restore.md` section 5. Run it before go-live.

## Security notes

Adapted from the critcomp hardening list, where it applies to this stack:

- **Secrets**: no secret is in the compose file or the image. The admin password is
  printed once; the handoff file is mode 600 and deleted after `install.sh` reads it. Keep
  backups of the DB and the blobs private: they contain user accounts and copyrighted PDFs.
- **Container**: non-root UID 10001, `no-new-privileges`, memory cap, read-only application
  code (owned by root), no docker socket mounted, no privileged mode.
- **Proxy trust**: `STUDIO_TRUST_PROXY=1` is set. The app trusts `X-Forwarded-For` only
  because the proxy is the only client that can reach port 8770. Keep the firewall rule.
  Rate limiting for login is keyed on the forwarded client address, and it must not key on
  a spoofable header when no proxy is present.
- **Fail closed on the LAN**: `caddy-snippet.txt` aborts connections to `meta.home.mfendt.de`
  from outside the private ranges. Go-live is a separate, reviewed change.
- **Transport**: TLS terminates at the proxy (wildcard certificate today; Let's Encrypt for
  the public name at go-live). HSTS is set by the proxy. Requests are limited to 250 MB.
- **Passkeys are origin-bound** (WebAuthn). Register them only on the final domain
  (`meta.mfendt.de` at go-live). A passkey created on the LAN name will not work on the
  public one.
- **Token comparison** must be constant-time in the app (`hmac.compare_digest`). Login
  failures are rate-limited.
- **Logs**: the app writes no passwords or tokens. Rotate them with the json-file limits
  above. Audit trail: every write is recorded in the `audit` table.
- **Data protection**: the corpus contains copyrighted PDFs in per-user access control. The
  owner has to confirm the arrangement with the university's data-protection office before
  the public go-live.
- **Updates**: pull the repo, rebuild, restart. Pin the base image digest when you need
  reproducible builds.
