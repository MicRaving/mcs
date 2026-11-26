# Studio server

The server side of CodingStudio's online projects. It is a small FastAPI application
(one process, one worker, SQLite in WAL mode) that stores the project's
accounts, members, sync change log, blobs (PDFs, datasets), locks, presence, assignments
and audit trail. Desktop clients and the browser client talk to it over HTTPS.

**One product, one image.** The sync server and browser mode are not two services any
more: `Server/Dockerfile` builds one image, `python -m studio_server run` is its
entrypoint, and `STUDIO_BROWSER_MODE` (`0` default, `1` = on) decides whether the browser
gateway and its per-user workers run beside the sync server. With the switch off no
browser module is imported: the product is the 52–57 MB sync server alone.

Requirements: 256 MB and one core for sync-only; 1.5 GB and 2–4 cores with browser mode
(that is the container's own cap — the supervisor derives its worker cap and pressure
ceiling from it). A small rootfs and a separate large volume for blobs.

## Layout

```
Server/
  studio_server/        the sync server (FastAPI) and the product's entrypoint:
                        run.py = sync server + supervised browser gateway
  studio_web/           the browser gateway (`python -m studio_web`): login cookie,
                        project picker, proxy to one worker per (user, project),
                        worker lifecycle, heavy lane (PLAN_online_only §4/§6)
  tests/                pytest suite (not shipped in the image)
  studio_web/tests/     gateway unit tests + the browser e2e and leak-sweep suites
  requirements.txt      runtime + dev dependencies (sync server)
  requirements-prod.txt sync-server runtime dependencies (used by Server/Dockerfile)
  requirements-web.txt  browser-mode runtime dependencies (same image)
  Dockerfile            THE image: sync server + browser mode, built from the repo root
  Dockerfile.dockerignore   build-context allow-list
  docker-compose.yml    one service (`studio`) + optional bundled caddy profile
  deploy/
    meta-lxc.sh         Proxmox host: creates the LXC CT and installs the stack in it
    pve-oci.sh          Proxmox host: the image as an OCI application container (no Docker)
    install.sh          inside the CT: Docker, data dirs, stack, first admin, backup cron
    upgrade.sh          inside the CT: swap in a new source tarball, rebuild, health wait
    health-check.sh     status check (exit 1 on failure)
    backup.sh           nightly SQLite snapshot, browser outboxes, blob mirror, off-site, retention
    restore.md          restore runbook and restore drill
    caddy-snippet.txt   site block for the existing proxy on CT 110 (browser mode included)
    caddy-browser-mode.txt  the exact change from sync-only to one origin with /app/
    Caddyfile.bundled   proxy for installs without an existing proxy
```

The server imports `online_shared` (`Scripts/online_shared/`, stdlib only, shared with the
desktop client). In development this is found by adding `<repo>/Scripts` to `sys.path`
(see `studio_server/__init__.py`). In the image it is copied to `/app/online_shared`.

## Local development

Run from `Server/`. Use a scratch data directory outside the repo's data folders:

```bash
cd Server
python -m pip install -r requirements.txt    # sync server only
python -m studio_server create-admin          # reads ADMIN_USER / ADMIN_PASSWORD from the environment
# the whole product, browser mode off (the default): :8770 + the /app/ notice on :8780
python -m studio_server run --host 127.0.0.1 --port 8770 --data ../temp/online/server-dev
# or with browser mode (needs requirements-web.txt too):
STUDIO_BROWSER_MODE=1 python -m studio_server run --host 127.0.0.1 --port 8770 \
  --data ../temp/online/server-dev
curl http://127.0.0.1:8770/api/health
```

Tests:

```bash
cd Server
PYTHONPYCACHEPREFIX=../temp/pycache python -m pytest tests
```

Keep scratch data under `temp/` (the project's rule for intermediates).

## Docker / Podman (one container)

BuildKit is required. It is the default in current Docker (`DOCKER_BUILDKIT=1` only
matters on old versions). `Server/Dockerfile.dockerignore` is an allow-list and BuildKit
reads it because it sits next to the Dockerfile. Without BuildKit the whole repo, including
the large data folders, would be sent as the build context.

```bash
# from the repo root
docker build -f Server/Dockerfile -t studio-server:local .

# from Server/: one container, browser mode ON (the compose default)
docker compose up -d --build

# sync only (~52 MB, one core) — the same container, the same ports
STUDIO_BROWSER_MODE=0 docker compose up -d --build

# plus the bundled Caddy (no external proxy)
STUDIO_DOMAIN=meta.example.org docker compose --profile caddy up -d
```

The container runs as UID 10001 (non-root, no login shell), with `no-new-privileges`, a
read-only rootfs and logs rotated at 10 MB × 3. The host directories `/srv/studio/data`,
`/srv/studio/web` and `/data/blobs` must be owned by 10001 (`install.sh` sets this; for a
hand-made `docker run`, `chown -R 10001:10001` them first, or the server cannot write).
Data lives at `/data`, blobs at `/data/blobs`, browser mirrors at `/srv/studio/web`.

Ports 8770 (sync API) and 8780 (`/app/`) are published on the host. The stack assumes the
firewall allows them only from the proxy host, because the server trusts `X-Forwarded-For`
(`STUDIO_TRUST_PROXY=1`). With the bundled Caddy profile, remove the `ports:` entries once
the proxy is in place.

## Browser mode (the `STUDIO_BROWSER_MODE` switch)

Browser mode is not a second service. The one image contains both roles: the gateway
(`Server/studio_web`, FastAPI + httpx) and the headless CodingStudio worker
(`Scripts/coding_web/browser_worker.py`: the desktop app in online mode, one process per
(user, project), started as a forkserver child of the gateway). `python -m studio_server run`
starts the sync server and — only when `STUDIO_BROWSER_MODE=1` — the gateway as a supervised
child in the same container. PLAN_online_only §7–§8 is the design; the measured numbers are
in `Results/ONE_SERVER_PRODUCT_memory.md`.

Turning it on and off:

| place | how |
|---|---|
| Docker/Podman | `STUDIO_BROWSER_MODE=1` (compose default) or `0`; recreate the container |
| Proxmox OCI CT | `STUDIO_BROWSER_MODE=0`/`1` in the CT environment; `pct set <id> --memory 256` + `pct stop/start`, or `Server/deploy/pve-oci.sh` with/without `--browser` |
| Proxmox Docker CT | edit the variable in `/opt/studio/Server/docker-compose.yml` (or export it) and `docker compose up -d` |
| without browser mode | `http://<host>:8780/app/` answers a friendly page: "Browser mode is switched off on this server — use the desktop app" |

The reverse-proxy configuration is identical either way: `/app` and `/app/*` always go to
port 8780 (the gateway when on, the notice page from the sync process when off), everything
else to 8770. `GET /api/health` reports `browser_mode: "on"|"off"` and, when on, `workers`,
`rss_mb` and `gateway_ok`.

| | value | why |
|---|---|---|
| image | 658 MB: `python:3.11-slim` + Flask, numpy, scipy, pandas, openpyxl, PyMuPDF, python-docx, python-pptx, lxml, Pillow + fastapi/uvicorn/httpx | `requirements-web.txt` + `requirements-prod.txt`; matplotlib, pyreadstat and pyreadr stay out and their routes degrade as they already do on the desktop |
| user | UID 10001, read-only rootfs, `tmpfs /tmp`, `no-new-privileges` | same shape as before; the app writes only into `/data`, `/srv/studio/web` |
| `/srv/studio/web` | mirrors + per-user homes + caches (mp1, local-lvm, `backup=0`) | thousands of small reads, never the extHDD; rebuildable, so not in the vzdump |
| `/data/blobs` | the blob store | mirrors symlink into it (`STUDIO_WEB_LOCAL_BLOBS`), so a paper is never downloaded twice |
| server URL | `STUDIO_WEB_SERVER=http://127.0.0.1:8770` | the gateway talks to the sync server in the same container |
| ports | 8770 and 8780 published | firewall both to the proxy host only |
| memory | `mem_limit: 2g` by default, `memswap_limit: 2600m`, `pids_limit: 256` | the browser side shares this cgroup with the sync server, so it can never starve it; the supervisor recycles workers before the limit |
| worker lifecycle | auto from the cgroup: `pressure_mb` = limit − 15 %, `soft/hard` = 40 %/72 % of the limit, `idle_secs` 900 (≥1.4 GB) / 600 / 300 / 120, `max_workers` = (limit − 192 MB) / 384 MB, capped at 8 | overrides: `STUDIO_WEB_SOFT_MB`, `STUDIO_WEB_HARD_MB`, `STUDIO_WEB_PRESSURE_MB`, `STUDIO_WEB_IDLE_SECS`, `STUDIO_WEB_MAX_WORKERS` |
| forkserver | preloads Flask + the app + numpy/pandas/PyMuPDF/lxml/Pillow once, then `gc.freeze()`; scipy/openpyxl/pptx/docx are imported lazily by the routes that need them | all workers share one copy of the imports (copy-on-write), and a worker that only codes never pays for scipy (~14 MB private per worker less) |
| session | `STUDIO_WEB_SESSION_DAYS` 30, `STUDIO_WEB_COOKIE_SECURE` 1, `STUDIO_WEB_PUBLIC_ORIGIN` | the cookie is `HttpOnly; Secure; SameSite=Strict; Path=/app` and holds the server's own token, so there is no second credential store |

Health: `GET http://127.0.0.1:8780/app/health` returns `{ok, workers, rss_mb, lane,
launcher, limits}`; the per-worker detail (PID, PSS, inflight, warm families, the last
lifecycle events) is added for loopback clients only, which is why `health-check.sh` asks
from inside the container. `GET :8770/api/health` carries the same browser summary, so one
probe covers the whole product.

Routing (one origin; `deploy/caddy-snippet.txt`, exact delta in
`deploy/caddy-browser-mode.txt`):

```
https://meta.home.mfendt.de/app, /app/*   -> CT 112:8780   web     (login, projects, /app/p/<pid>/…)
https://meta.home.mfendt.de/…             -> CT 112:8770   studio  (sync API, unchanged)
https://meta.home.mfendt.de/              -> 302 /app/
```

The LAN-only matcher (`@meta_public` + `abort`) covers both backends: browser mode does not
widen who can reach the service. Validate the Caddyfile before reloading (`caddy validate`),
keep a timestamped copy, and reload only if the validation passed.

Tests:

```bash
# from the repo root (PowerShell: $env:STUDIO_E2E="1")
STUDIO_E2E=1 python -m pytest Server/studio_web/tests -q
python -m pytest Server/tests -q
python -m pytest Server/studio_web/tests -q
python -m pytest Scripts/tests -q
python -m pytest Scripts/coding_web/tests_static -q
```

`Server/tests/test_run.py` covers the toggle (off imports no browser module, the `/app/`
notice, health on/off, gateway restart with backoff) and `Server/studio_web/tests/
test_config_limits.py` the memory-limit-derived worker policy. Run the four directories
separately: `Server/tests/conftest.py` and `Scripts/coding_web/tests_static/conftest.py`
share a module name, so one pytest invocation over all of them cannot collect.

`Server/studio_web/tests/test_e2e_browser.py` is the parity suite (login, blinding, coder
gate, route policy, locks, Finish paper, save authorship); `test_leak_sweep.py` walks every
GET route of the app's URL map as a blinded coder and asserts that no response body carries
another tag's values, the AI verdicts or the derived products.

Upgrade: `bash /opt/studio/Server/deploy/upgrade.sh /root/studio-src.tar.gz` (see
`deploy/upgrade.sh`; it keeps `/opt/studio.prev` and prints the rollback command).

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

1. Paste `Server/deploy/caddy-snippet.txt` into the Caddyfile on CT 110 and reload Caddy. The
   exact change from a sync-only block (one `handle` → `/app/` to the gateway, everything else to
   the sync server, `redir / /app/ 302`) is in `Server/deploy/caddy-browser-mode.txt`; validate
   before reloading (`caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`).
2. Make sure `meta.home.mfendt.de` resolves on the LAN (wildcard, or an A record on CT 106).
3. Firewall TCP 8770 **and 8780** on CT 112 so only 192.168.178.10 can reach them.
4. Check: `pct exec 112 -- bash /opt/studio/Server/deploy/health-check.sh` (it covers the sync
   server and the browser gateway; it also proves that uid 10001 can read the blob store).
5. Log in at `https://meta.home.mfendt.de/app/` from the LAN (desktop clients keep using the sync
   API at the same origin). Outside the LAN the request gets no response.
6. With browser mode the first login of a member links the project's core tier into
   `/srv/studio/web/u<id>/` (seconds, not a download). A **blinded** member needs
   `Project/master_records.csv.gz` in the project - `Scripts/compile_master.py` writes it on
   every compile; without it the abstract tab has no rows to show.

Upgrade an existing CT with `bash /opt/studio/Server/deploy/upgrade.sh <tarball>` (it swaps the
checkout, rebuilds the image, waits for the health endpoints and prints the rollback command).

Docker in an unprivileged LXC sometimes fails with AppArmor errors. If that happens, add
`lxc.apparmor.profile: unconfined` to `/etc/pve/lxc/112.conf`, restart the CT, and note the
reason in the ops log.

### Proxmox OCI application container (no Docker)

PVE 9 imports an OCI archive as an unprivileged LXC whose PID 1 is the image's
ENTRYPOINT/CMD, with `USER` mapped to `lxc.init.uid`/`gid`. That removes Docker from the
guest entirely (~165 MB of daemons) and keeps the server a single process. The helper is
`Server/deploy/pve-oci.sh`, run on the PVE host as root.

```bash
# once per new image: export it from a Docker-capable CT (default 112, only a build)
bash pve-oci.sh build  --ct 112 --src /opt/studio --image studio-server:local
bash pve-oci.sh export --image studio-server:local

# a sync-only CT: 256 MB, 1 core, onboot, /data + /data/blobs + /srv/studio/web mounted
bash pve-oci.sh create --ctid 196 --image studio-server:local

# browser mode: 1.5 GB, 2 cores
bash pve-oci.sh create --ctid 197 --image studio-server:local --browser \
     --public-origin https://meta.home.mfendt.de

# a release image from GHCR (PVE has no registry client; the build CT pulls it)
bash pve-oci.sh fetch  --image ghcr.io/<owner>/<repo>-server:v1.2.3

# new CT on the same data/mirror mounts, old CT kept stopped for rollback
bash pve-oci.sh upgrade  --ctid 196 --image studio-server:local
bash pve-oci.sh rollback --ctid 196
bash pve-oci.sh status   --ctid 196
bash pve-oci.sh destroy  --ctid 198 --purge
```

`create`/`upgrade` export with `docker save --platform linux/amd64` (required: Docker's
containerd store otherwise writes an index with attestation manifests and PVE refuses it
with "Unsupported CPU architecture"), pull the archive into
`/var/lib/vz/template/cache/`, create the CT with the three mount points, apply the
environment, start it and wait for `/api/health`. Every default is in the image; overrides
are `--env KEY=VALUE` (repeatable) and are written as `lxc.environment.runtime` lines,
because PVE's own `--env` is a NUL-separated list a shell cannot pass.
`STUDIO_MEM_LIMIT_MB` is set from the CT's memory automatically: inside an LXC the
container's `memory.max` is not always visible, so the worker cap needs the number.

The host directories are created once and owned by `110001` (unprivileged root `100000` +
the image's uid `10001`); they are shared by the CT and its upgrade CT, so **never start
two of these CTs against the same data dir**. `upgrade` stops the old CT before the new one
starts, moves `onboot` to the new CT and records the pair in `/root/studio-oci/rollback`.

Settings (`pve-oci.sh`, defaults in brackets): `--ct` build CT [112], `--image`
[studio-server:local], `--ctid` [next free from 200], `--browser` [off], `--memory`
[256 sync / 1536 browser], `--cores` [1 / 2], `--rootfs-size` [6 GB], `--data`
[/srv/studio-oci], `--ip` [dhcp], `--bridge` [vmbr0], `--public-origin`, `--env`,
`--no-onboot`. `BUILD_CT`, `MEMORY_SYNC`, `MEMORY_BROWSER`, `ROOTFS_SIZE` and the rest can
also be set as environment variables.

If you would rather not use the helper, the equivalent is three commands:

```bash
pct exec 112 -- docker save --platform linux/amd64 studio-server:local -o /root/x.tar
pct pull 112 /root/x.tar /var/lib/vz/template/cache/studio-oci.tar
pct create 196 local:vztmpl/studio-oci.tar --memory 256 --swap 0 --cores 1 \
  --unprivileged 1 --rootfs local-lvm:6 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --mp0 /srv/studio-oci/data,mp=/data --mp1 /srv/studio-oci/blobs,mp=/data/blobs \
  --mp2 /srv/studio-oci/web,mp=/srv/studio/web --onboot 1 --start 1
```

### Settings

`meta-lxc.sh` reads these environment variables (defaults in brackets):

| variable | meaning |
|---|---|
| `CTID` `CT_HOSTNAME` | container id [112], hostname [meta] |
| `CORES` `RAM_MB` `SWAP_MB` `DISK` | [4] [2048] [512] [16G] |
| `STORAGE` `BLOB_STORAGE` `BLOB_SIZE` | rootfs storage [local-lvm], blob storage [extHDD], [200G] |
| `WEB_STORAGE` `WEB_SIZE` `WEB_BIND` | browser mirror volume (mp1) storage [= STORAGE], [20G], host dir instead of a volume [unset] |
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
| restore | `Server/deploy/restore.md` (DB only, blobs, browser mirrors, whole CT, drill) |
| upgrade | `bash /opt/studio/Server/deploy/upgrade.sh /root/studio-src.tar.gz` |
| admin password reset | `cd /opt/studio/Server && docker compose exec studio python -m studio_server reset-password` |

Upgrade notes: the schema is migrated on start (`PRAGMA user_version`). Take a backup
before an upgrade that changes the schema. `install.sh` is also safe to re-run.

### Backups

- **DB snapshots**: nightly 02:30 by cron (`/etc/cron.d/studio-backup`). Each snapshot is an
  online SQLite backup, checked with `PRAGMA integrity_check`. Kept 30 days in
  `BACKUP_DIR`.
- **Browser mirrors**: not backed up. `backup.sh` first asks the gateway to drain every
  worker (`POST /app/admin/drain`, loopback inside the container), then keeps only every
  `<mirror>/.studio/` (sync state, unsent outbox, queued versions) in
  `web-state-<stamp>.tar.gz`. A lost mirror is rebuilt by the next login.
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
  because the proxy is the only client that can reach ports 8770 and 8780. Keep the firewall
  rules. Rate limiting for login is keyed on the forwarded client address, and it must not
  key on a spoofable header when no proxy is present.
- **Browser session**: the gateway has no password store; the cookie carries the sync
  server's opaque token (`HttpOnly; Secure; SameSite=Strict; Path=/app`), so a revocation or
  password reset takes effect within 60 s (`GET /api/auth/me` cache). Every non-GET request
  must carry the site's `Origin`. A worker listens on `127.0.0.1` only and refuses requests
  without its per-worker secret, so no other process in the container (or user of the
  service) can drive it.
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
