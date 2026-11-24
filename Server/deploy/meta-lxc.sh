#!/usr/bin/env bash
# meta-lxc.sh - create the studio-server LXC (CT) on a Proxmox VE host and install the stack in it.
#
# Run on the Proxmox host as root. Refuses to run if CTID already exists.
#
#   REPO_SOURCE=https://github.com/<owner>/<repo>.git REPO_TOKEN=<fine-grained PAT, Contents: read> \
#     bash meta-lxc.sh
#   DRY_RUN=1 REPO_SOURCE=... bash meta-lxc.sh      # prints the pct commands, changes nothing
#
# Env overrides (defaults in parentheses):
#   CTID (112)  CT_HOSTNAME (meta)  CORES (4)  RAM_MB (2048)  SWAP_MB (512)
#   DISK (16G)  STORAGE (local-lvm)  BLOB_STORAGE (extHDD)  BLOB_SIZE (200G)
#   WEB_STORAGE (STORAGE)  WEB_SIZE (20G)  WEB_BIND (unset: a volume, or a host dir)
#   BRIDGE (vmbr0)  IP (192.168.178.12/24)  GW (192.168.178.1)
#   TEMPLATE (local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst)
#   REPO_SOURCE   required: git URL, or https URL of a .tar.gz/.tgz/.tar.xz
#   REPO_REF      optional branch or tag
#   REPO_TOKEN    optional fine-grained PAT (Contents: read) for a private repo;
#                 it is sent as an HTTP header, never put into a URL or a command line
#   ADMIN_USER (admin)  ADMIN_PASSWORD (generated and printed once if unset)
#   BACKUP_DIR (/srv/studio/backups)  BLOB_BACKUP_DIR (unset: no blob mirror)  RCLONE_REMOTE (unset)
#   STUDIO_CADDY (0; 1 = bundled Caddy profile)  STUDIO_DOMAIN (meta.home.mfendt.de)
#   DRY_RUN (0)

set -euo pipefail

log() { printf '[meta-lxc %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf '[meta-lxc] ERROR: %s\n' "$*" >&2; exit 1; }

CTID="${CTID:-112}"
CT_HOSTNAME="${CT_HOSTNAME:-meta}"
CORES="${CORES:-4}"
RAM_MB="${RAM_MB:-2048}"
SWAP_MB="${SWAP_MB:-512}"
DISK="${DISK:-16G}"
STORAGE="${STORAGE:-local-lvm}"
BLOB_STORAGE="${BLOB_STORAGE:-extHDD}"
BLOB_SIZE="${BLOB_SIZE:-200G}"
# BLOB_BIND: host directory bind-mounted at /data/blobs instead of a volume on
# BLOB_STORAGE (use it when that storage does not allow container volumes,
# e.g. a "dir" storage whose content is only "images").  Owned on the host by
# 110001 = the unprivileged CT's uid 10001 (the server container's user).
BLOB_BIND="${BLOB_BIND:-}"
# WEB: the browser mode mirrors + per-user caches (PLAN_online_only §8).  A separate
# volume (mp1) keeps them off the rootfs: the rootfs holds the OS, Docker and two
# images; the mirrors are thousands of small reads on local-lvm, never the extHDD.
WEB_STORAGE="${WEB_STORAGE:-$STORAGE}"
WEB_SIZE="${WEB_SIZE:-20G}"
WEB_BIND="${WEB_BIND:-}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-192.168.178.12/24}"
GW="${GW:-192.168.178.1}"
TEMPLATE="${TEMPLATE:-local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst}"
REPO_SOURCE="${REPO_SOURCE:-}"
REPO_REF="${REPO_REF:-}"
REPO_TOKEN="${REPO_TOKEN:-}"
DRY_RUN="${DRY_RUN:-0}"

# ---- validate ---------------------------------------------------------------
[[ "$CTID" =~ ^[0-9]+$ ]] || die "CTID must be a number"
[[ "$DISK" =~ ^[0-9]+G$ ]] || die "DISK must look like 16G"
[[ "$BLOB_SIZE" =~ ^[0-9]+G$ ]] || die "BLOB_SIZE must look like 200G"
[[ "$WEB_SIZE" =~ ^[0-9]+G$ ]] || die "WEB_SIZE must look like 20G"
DISK_GB="${DISK%G}"
BLOB_GB="${BLOB_SIZE%G}"
WEB_GB="${WEB_SIZE%G}"
case "$IP" in */*) ;; *) die "IP must be CIDR, e.g. 192.168.178.12/24" ;; esac
[ -n "$REPO_SOURCE" ] || die "REPO_SOURCE is required (git URL or tarball URL)"
TEMPLATE_STORAGE="${TEMPLATE%%:*}"
TEMPLATE_FILE="${TEMPLATE#*vztmpl/}"

# ---- environment checks -----------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  log "DRY RUN: the pct commands are printed, nothing is changed"
else
  [ "$(id -u)" -eq 0 ] || die "run as root on the Proxmox host"
  for c in pct pveam; do
    command -v "$c" >/dev/null 2>&1 || die "$c not found: run this on the Proxmox host"
  done
fi

if command -v pct >/dev/null 2>&1 && pct status "$CTID" >/dev/null 2>&1; then
  die "container $CTID already exists; refusing to touch it"
fi

if [ "$DRY_RUN" != "1" ] && ping -c1 -W2 "${IP%/*}" >/dev/null 2>&1; then
  die "${IP%/*} already answers ping; choose another IP"
fi

WORK="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK"
  if [ "$DRY_RUN" != "1" ]; then
    pct exec "$CTID" -- rm -f /root/.studio-install.env /root/.studio-repo-header /root/ct-bootstrap.sh >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# run: execute, or print the command in dry-run mode
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '  [dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

wait_for() {
  local what="$1" tries="$2"
  shift 2
  local i
  for i in $(seq 1 "$tries"); do
    if "$@" >/dev/null 2>&1; then
      log "$what: ready"
      return 0
    fi
    sleep 3
  done
  die "$what: not ready after $tries tries"
}

# ---- template ---------------------------------------------------------------
if [ "$DRY_RUN" != "1" ]; then
  if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '{print $1}' | grep -qxF "$TEMPLATE"; then
    log "template $TEMPLATE not present; downloading"
    pveam update >/dev/null 2>&1 || true
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE_FILE"
  fi
else
  log "dry-run: would ensure template $TEMPLATE is present"
fi

# ---- create and start -------------------------------------------------------
log "creating CT $CTID ($CT_HOSTNAME): ${CORES} cores, ${RAM_MB} MB RAM, ${SWAP_MB} MB swap, ${DISK} rootfs on $STORAGE"
log "  blob store: ${BLOB_SIZE} on $BLOB_STORAGE at /data/blobs; browser mirrors: ${WEB_SIZE} on $WEB_STORAGE at /srv/studio/web"
# nesting + keyctl: Docker inside an unprivileged LXC. The blob volume is excluded from vzdump
# (backup=0): blobs are protected by BLOB_BACKUP_DIR and/or RCLONE_REMOTE, see restore.md.
if [ -n "$BLOB_BIND" ]; then
  MP0="${BLOB_BIND},mp=/data/blobs,backup=0"
  run mkdir -p "$BLOB_BIND"
  run chown 110001:110001 "$BLOB_BIND"
else
  MP0="${BLOB_STORAGE}:${BLOB_GB},mp=/data/blobs,backup=0"
fi
# The browser mirrors are rebuildable from the server (mirror + .studio state), so
# they are excluded from the vzdump too; backup.sh keeps only the unpushed outboxes.
if [ -n "$WEB_BIND" ]; then
  MP1="${WEB_BIND},mp=/srv/studio/web,backup=0"
  run mkdir -p "$WEB_BIND"
  run chown 110001:110001 "$WEB_BIND"
else
  MP1="${WEB_STORAGE}:${WEB_GB},mp=/srv/studio/web,backup=0"
fi
run pct create "$CTID" "$TEMPLATE" \
  --hostname "$CT_HOSTNAME" --ostype debian \
  --cores "$CORES" --memory "$RAM_MB" --swap "$SWAP_MB" \
  --rootfs "${STORAGE}:${DISK_GB}" \
  --mp0 "$MP0" \
  --mp1 "$MP1" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=${IP},gw=${GW}" \
  --features "nesting=1,keyctl=1" \
  --unprivileged 1 --onboot 1 --start 0
run pct start "$CTID"

if [ "$DRY_RUN" != "1" ]; then
  wait_for "container exec" 60 pct exec "$CTID" -- true
  wait_for "container network" 30 pct exec "$CTID" -- getent hosts deb.debian.org
  pct exec "$CTID" -- mountpoint -q /data/blobs || die "/data/blobs is not mounted in CT $CTID (check mp0)"
  log "blob store mounted at /data/blobs"
  pct exec "$CTID" -- mountpoint -q /srv/studio/web || die "/srv/studio/web is not mounted in CT $CTID (check mp1)"
  log "browser mirror volume mounted at /srv/studio/web"
fi

# ---- hand the repo and the settings to the container ------------------------
if [ -n "$REPO_TOKEN" ]; then
  printf 'Authorization: Basic %s\n' "$(printf 'x-access-token:%s' "$REPO_TOKEN" | base64 | tr -d '\n')" > "$WORK/repo-header"
  chmod 600 "$WORK/repo-header"
  run pct push "$CTID" "$WORK/repo-header" /root/.studio-repo-header --perms 600
fi

: > "$WORK/install.env"
chmod 600 "$WORK/install.env"
for v in ADMIN_USER ADMIN_PASSWORD BACKUP_DIR BLOB_BACKUP_DIR RCLONE_REMOTE STUDIO_CADDY STUDIO_DOMAIN; do
  if [ -n "${!v:-}" ]; then
    printf '%s=%q\n' "$v" "${!v}" >> "$WORK/install.env"
  fi
done
run pct push "$CTID" "$WORK/install.env" /root/.studio-install.env --perms 600

cat > "$WORK/ct-bootstrap.sh" <<'EOS'
#!/usr/bin/env bash
# Runs inside the CT: base packages and the repo checkout into /opt/studio.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl git tar

hdr=""
if [ -f /root/.studio-repo-header ]; then
  hdr="$(cat /root/.studio-repo-header)"
fi
rm -f /root/.studio-repo-header
rm -rf /opt/studio

fail_hint() { echo "checkout failed. For a private repo pass REPO_TOKEN (fine-grained PAT, Contents: read)." >&2; exit 1; }

case "$REPO_SOURCE" in
  /*)
    # a tarball on the Proxmox host, pushed into the CT as /root/studio-src.tar
    mkdir -p /opt/studio
    tar -x -f /root/studio-src.tar -C /opt/studio
    rm -f /root/studio-src.tar
    ;;
  *.tar.gz|*.tgz|*.tar.xz)
    mkdir -p /opt/studio
    if [ -n "$hdr" ]; then
      curl -fsSL -H "$hdr" "$REPO_SOURCE" | tar -x -C /opt/studio --strip-components=1 || fail_hint
    else
      curl -fsSL "$REPO_SOURCE" | tar -x -C /opt/studio --strip-components=1 || fail_hint
    fi
    ;;
  *)
    gargs=()
    if [ -n "$hdr" ]; then gargs=(-c "http.extraHeader=$hdr"); fi
    refargs=()
    if [ -n "${REPO_REF:-}" ]; then refargs=(--branch "$REPO_REF"); fi
    git ${gargs[@]+"${gargs[@]}"} clone --depth 1 ${refargs[@]+"${refargs[@]}"} "$REPO_SOURCE" /opt/studio || fail_hint
    ;;
esac

[ -f /opt/studio/Server/deploy/install.sh ] || { echo "checkout has no Server/deploy/install.sh" >&2; exit 1; }
echo "checkout ready: /opt/studio"
EOS
run pct push "$CTID" "$WORK/ct-bootstrap.sh" /root/ct-bootstrap.sh --perms 700
case "$REPO_SOURCE" in
  /*) [ "$DRY_RUN" = "1" ] || [ -f "$REPO_SOURCE" ] || die "REPO_SOURCE file $REPO_SOURCE not found"
      run pct push "$CTID" "$REPO_SOURCE" /root/studio-src.tar ;;
esac

log "installing base packages and checking out the repo (this takes a few minutes)"
run pct exec "$CTID" -- env "REPO_SOURCE=$REPO_SOURCE" "REPO_REF=$REPO_REF" bash /root/ct-bootstrap.sh

log "running install.sh inside CT $CTID"
run pct exec "$CTID" -- bash /opt/studio/Server/deploy/install.sh

# ---- summary ----------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  log "dry run finished: no changes were made"
  exit 0
fi

CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

======================================================
 studio server CT $CTID ($CT_HOSTNAME) ready
======================================================
 IP:           ${CT_IP:-?}  (configured: ${IP})
 health:       pct exec $CTID -- bash /opt/studio/Server/deploy/health-check.sh
 browser:      https://${STUDIO_DOMAIN:-meta.home.mfendt.de}/app/  (same accounts as the desktop)
 proxy:        paste Server/deploy/caddy-browser-mode.txt into the Caddyfile on CT 110
               (or set STUDIO_CADDY=1 for the bundled Caddy profile)
 firewall:     allow 8770 and 8780 only from the proxy host (192.168.178.10)
 backups:      nightly 02:30, DB snapshots in ${BACKUP_DIR:-/srv/studio/backups}
               (the mirror volume /srv/studio/web is rebuildable: only its outboxes are kept)
 restore:      Server/deploy/restore.md (in the CT: /opt/studio/Server/deploy/restore.md)
 upgrade:      Server/deploy/upgrade.sh (in the CT: /opt/studio/Server/deploy/upgrade.sh)
======================================================
EOF
