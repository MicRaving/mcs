#!/usr/bin/env bash
# pve-oci.sh - run the ONE CodingStudio server image as a Proxmox OCI application
# container (LXC, no Docker inside the container).
#
# Run on the Proxmox VE host as root.  PVE 9 imports an OCI archive as a normal
# unprivileged CT: the image's ENTRYPOINT/CMD become the CT's entrypoint (PID 1),
# USER becomes lxc.init.uid/gid and Env becomes lxc.environment.runtime.  The
# image is Server/Dockerfile - the same image Docker/Podman users run.
#
#   # build + export inside the Docker-capable CT (default 112, never touched
#   # beyond a build), then create a sync-only CT (256 MB / 1 core):
#   bash pve-oci.sh build  --ct 112 --src /opt/studio --image studio-server:local
#   bash pve-oci.sh create --ctid 196 --image studio-server:local
#
#   # browser mode on (the gateway + per-user workers, 1.5 GB / 2 cores):
#   bash pve-oci.sh create --ctid 197 --image studio-server:local --browser
#
#   # pull a release image from GHCR (PVE has no registry client; Docker inside
#   # the build CT pulls it, then the archive is exported):
#   bash pve-oci.sh fetch --image ghcr.io/<owner>/<repo>-server:latest
#
#   # upgrade: new export, new CT, same data/mirror mount points, old CT kept
#   # stopped for rollback
#   bash pve-oci.sh upgrade --ctid 196 --image studio-server:local
#   bash pve-oci.sh rollback --ctid 196
#
# Commands: build | export | fetch | create | upgrade | rollback | status | destroy
#
# The image carries every default as ENV, so a CT needs no environment at all
# for the normal case.  Overrides (append --env KEY=VALUE, repeatable) are
# written as lxc.environment.runtime lines in /etc/pve/lxc/<id>.conf: PVE's
# --env option is a NUL-separated list, which a shell cannot pass; the config
# file is the same thing PVE writes.  They are remembered per CT for upgrade.
set -euo pipefail

log() { printf '[pve-oci %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { printf '[pve-oci] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root on the Proxmox host"
command -v pct >/dev/null 2>&1 || die "pct not found: run this on the Proxmox host"

BUILD_CT="${BUILD_CT:-112}"                 # the CT with Docker (build/export only)
IMAGE="${IMAGE:-studio-server:local}"
STORE_DIR="${STORE_DIR:-/root/studio-oci}"  # per-CT records (env, mounts, template)
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"
STORAGE="${STORAGE:-local-lvm}"
DATA_BASE="${DATA_BASE:-/srv/studio-oci}"
CT_NAME="${CT_NAME:-studio}"
ONBOOT="${ONBOOT:-1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
ROOTFS_SIZE="${ROOTFS_SIZE:-6}"             # GB; the browser image extracts to ~2.5 GB
MEMORY_SYNC="${MEMORY_SYNC:-256}"
MEMORY_BROWSER="${MEMORY_BROWSER:-1536}"
CORES_SYNC="${CORES_SYNC:-1}"
CORES_BROWSER="${CORES_BROWSER:-2}"
SWAP="${SWAP:-0}"
CONTAINER_UID="${CONTAINER_UID:-110001}"    # unprivileged root (100000) + image uid 10001

# per-CT records and overrides
record_file() { echo "$STORE_DIR/$1.conf"; }
env_file() { echo "$STORE_DIR/$1.env"; }

usage() {
  sed -n '2,/^set -euo pipefail$/p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

next_ctid() {
  local id="${1:-200}"
  while pct status "$id" >/dev/null 2>&1; do id=$((id + 1)); done
  echo "$id"
}

template_name() {  # image -> a template-cache file name
  local name="$1"
  name="${name//\//-}"
  name="${name//:/-}"
  name="${name//./-}"
  echo "studio-oci-${name}.tar"
}

inside_ct() {  # ctid cmd...
  local ct="$1"; shift
  pct exec "$ct" -- "$@"
}

docker_ct() {  # run docker inside the Docker-capable CT
  inside_ct "$BUILD_CT" docker "$@"
}

# Export IMAGE from BUILD_CT to the host's template cache and print the
# PVE template volume id.  --platform is required: Docker's containerd image
# store otherwise exports an index with attestation manifests, and PVE refuses
# it ("Unsupported CPU architecture").
export_image() {
  local image="$1" file remote
  file="$(template_name "$image")"
  remote="/root/$file"
  local target="/var/lib/vz/template/cache/$file"
  if [ -f "$target" ] && [ "${FORCE_EXPORT:-0}" != "1" ]; then
    log "template present: $target (FORCE_EXPORT=1 to rebuild)"
    echo "$TEMPLATE_STORAGE:vztmpl/$file"
    return 0
  fi
  log "exporting $image from CT $BUILD_CT (docker save --platform linux/amd64)"
  inside_ct "$BUILD_CT" docker image inspect "$image" >/dev/null 2>&1 \
    || die "image $image is not in CT $BUILD_CT (run: $0 build)"
  inside_ct "$BUILD_CT" docker save --platform linux/amd64 "$image" -o "$remote"
  log "pulling the archive to /var/lib/vz/template/cache/$file"
  pct pull "$BUILD_CT" "$remote" "$target"
  pct exec "$BUILD_CT" -- rm -f "$remote"
  tar -tf "$target" | grep -qx 'oci-layout' \
    || die "$target is not an OCI archive (rebuild with docker save --platform linux/amd64)"
  log "template ready: $target ($(du -h "$target" | cut -f1))"
  echo "$TEMPLATE_STORAGE:vztmpl/$file"
}

ensure_dirs() {  # data blobs web
  local d
  for d in "$@"; do
    install -d -m 0750 "$d"
    find "$d" ! -user "$CONTAINER_UID" -exec chown "$CONTAINER_UID:$CONTAINER_UID" {} + 2>/dev/null || true
  done
}

# Override/add environment in the CT config.  One lxc.environment.runtime line
# per variable; keys named in OVERRIDE_KEYS replace the image's value.
apply_env() {
  local ctid="$1"; shift
  local conf="/etc/pve/lxc/$ctid.conf" line key
  [ -f "$conf" ] || die "no $conf"
  local keys=()
  for line in "$@"; do keys+=("${line%%=*}"); done
  # drop the image's own value for keys we override (last assignment would win
  # anyway, but a duplicate in the config is confusing to read)
  for key in ${keys[@]+"${keys[@]}"}; do
    sed -i "/^lxc\.environment\.runtime: ${key}=/d" "$conf"
  done
  for line in "$@"; do
    printf 'lxc.environment.runtime: %s\n' "$line" >> "$conf"
    log "env: $line"
  done
}

wait_health() {  # ctid port
  local ctid="$1" port="${2:-8770}" i
  for i in $(seq 1 $((HEALTH_TIMEOUT / 3))); do
    if pct exec "$ctid" -- python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$port/api/health', timeout=4)" \
        >/dev/null 2>&1; then
      log "CT $ctid: /api/health ok"
      return 0
    fi
    sleep 3
  done
  return 1
}

ct_ip() { pct exec "$1" -- hostname -I 2>/dev/null | awk '{print $1}'; }

save_record() {  # ctid browser template data blobs web memory cores
  local ctid="$1" browser="$2" template="$3" data="$4" blobs="$5" web="$6"
  local memory="${7:-}" cores="${8:-}"
  mkdir -p "$STORE_DIR"; chmod 700 "$STORE_DIR"
  cat > "$(record_file "$ctid")" <<EOF
CTID=$ctid
BROWSER=$browser
TEMPLATE=$template
DATA_DIR=$data
BLOBS_DIR=$blobs
WEB_DIR=$web
MEMORY=$memory
CORES=$cores
IP=$IP
BRIDGE=$BRIDGE
STORAGE=$STORAGE
ROOTFS=$ROOTFS_SIZE
EOF
  chmod 600 "$(record_file "$ctid")"
}

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

cmd_build() {
  local src="${SRC:-/opt/studio}" image="$IMAGE"
  [ -d "$STORE_DIR" ] || mkdir -p "$STORE_DIR"
  log "building $image inside CT $BUILD_CT from $src"
  pct exec "$BUILD_CT" -- sh -c "cd '$src' && docker build --platform linux/amd64 -f Server/Dockerfile -t '$image' ."
  log "built: $(docker_ct image inspect -f '{{.Id}} {{.Size}} bytes' "$image" 2>/dev/null || echo "$image")"
}

cmd_export() {
  export_image "$IMAGE"
}

cmd_fetch() {
  # pull a registry image (e.g. the GHCR release build) into the build CT, then
  # export it; PVE itself has no registry client.
  local image="$1"
  [ -n "$image" ] || die "usage: $0 fetch ghcr.io/<owner>/<repo>-server:<version>"
  log "pulling $image in CT $BUILD_CT"
  docker_ct pull --platform linux/amd64 "$image"
  export_image "$image"
}

cmd_create() {
  local ctid="${CTID:-}" browser="${BROWSER:-0}" image="$IMAGE"
  # each host directory can be named on its own (e.g. the blob store on an
  # external disk: BLOBS_DIR=/mnt/extHDD/studio-blobs)
  local data="${DATA_DIR:-$DATA_BASE/data}" blobs="${BLOBS_DIR:-$DATA_BASE/blobs}"
  local web="${WEB_DIR:-$DATA_BASE/web}"
  local memory cores
  if [ "$browser" = "1" ]; then memory="$MEMORY_BROWSER"; cores="$CORES_BROWSER"
  else memory="$MEMORY_SYNC"; cores="$CORES_SYNC"; fi
  [ -n "$ctid" ] || ctid="$(next_ctid 200)"
  pct status "$ctid" >/dev/null 2>&1 && die "CT $ctid already exists"

  local template; template="$(export_image "$image")"

  ensure_dirs "$data" "$blobs" "$web"
  log "creating CT $ctid ($CT_NAME): ${memory} MB, ${cores} core(s), rootfs ${ROOTFS_SIZE}G on $STORAGE"
  log "  mounts: $data -> /data, $blobs -> /data/blobs, $web -> /srv/studio/web"
  pct create "$ctid" "$template" \
    --hostname "$CT_NAME" --ostype debian \
    --cores "$cores" --memory "$memory" --swap "$SWAP" \
    --rootfs "${STORAGE}:${ROOTFS_SIZE}" \
    --mp0 "$data,mp=/data" \
    --mp1 "$blobs,mp=/data/blobs" \
    --mp2 "$web,mp=/srv/studio/web" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=${IP}" \
    --unprivileged 1 --onboot "$ONBOOT" --start 0

  # STUDIO_MEM_LIMIT_MB: inside an LXC the container's cgroup is not always
  # visible as memory.max, so state the CT's memory the supervisor must plan for
  local envs=("STUDIO_BROWSER_MODE=$browser" "STUDIO_MEM_LIMIT_MB=$memory")
  envs+=(${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"})
  apply_env "$ctid" "${envs[@]}"
  save_record "$ctid" "$browser" "$template" "$data" "$blobs" "$web" "$memory" "$cores"
  printf '%s\n' "${envs[@]}" > "$(env_file "$ctid")"

  log "starting CT $ctid"
  pct start "$ctid"
  if ! wait_health "$ctid" 8770; then
    die "CT $ctid did not become healthy; it is left running for inspection (pct console $ctid)"
  fi
  if [ "$browser" = "1" ]; then
    if pct exec "$ctid" -- python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8780/app/health', timeout=4)"; then
      log "CT $ctid: browser gateway ok on :8780"
    else
      log "WARN CT $ctid: the browser port is not answering yet (a crashed gateway is restarted; see the log)"
    fi
  fi
  log "CT $ctid ready: http://$(ct_ip "$ctid" || echo '?'):8770/api/health   (browser mode: $browser)"
}

cmd_upgrade() {
  local old="${CTID:-}" image="$IMAGE"
  [ -n "$old" ] || die "usage: $0 upgrade --ctid <old CT> [--image <image>]"
  local rec; rec="$(record_file "$old")"
  [ -f "$rec" ] || die "no record for CT $old ($rec); was it created by pve-oci.sh?"
  # shellcheck disable=SC1090
  . "$rec"
  local browser="$BROWSER" data="$DATA_DIR" blobs="$BLOBS_DIR" web="$WEB_DIR"
  local memory="$MEMORY" cores="$CORES"
  local new; new="$(next_ctid $((old + 1)))"
  local template; template="$(export_image "$image")"

  ensure_dirs "$data" "$blobs" "$web"
  log "upgrade $old -> $new (same data/mirrors, old CT kept for rollback)"
  pct create "$new" "$template" \
    --hostname "$CT_NAME" --ostype debian \
    --cores "$cores" --memory "$memory" --swap "$SWAP" \
    --rootfs "${STORAGE}:${ROOTFS_SIZE}" \
    --mp0 "$data,mp=/data" --mp1 "$blobs,mp=/data/blobs" --mp2 "$web,mp=/srv/studio/web" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=${IP}" \
    --unprivileged 1 --onboot "$ONBOOT" --start 0
  local envs=("STUDIO_BROWSER_MODE=$browser" "STUDIO_MEM_LIMIT_MB=$memory")
  if [ -f "$(env_file "$old")" ]; then
    mapfile -t saved < "$(env_file "$old")"
    envs=("${saved[@]}")
    # a CT upgraded with more/less memory keeps the new figure
    envs=($(printf '%s\n' "${envs[@]}" | grep -v '^STUDIO_MEM_LIMIT_MB='))
    envs+=("STUDIO_MEM_LIMIT_MB=$memory")
  fi
  apply_env "$new" "${envs[@]}"
  save_record "$new" "$browser" "$template" "$data" "$blobs" "$web" "$memory" "$cores"
  printf '%s\n' "${envs[@]}" > "$(env_file "$new")"

  log "stopping CT $old (SQLite must not be opened by two CTs)"
  pct stop "$old" || true
  pct set "$new" --onboot 1 >/dev/null
  pct start "$new"
  if ! wait_health "$new" 8770; then
    log "new CT $new is not healthy; rolling back"
    pct stop "$new" || true
    pct start "$old"
    die "upgrade failed; CT $old is running again (CT $new left stopped for inspection)"
  fi
  pct set "$old" --onboot 0 >/dev/null
  cat > "$STORE_DIR/rollback" <<EOF
NEW=$new
OLD=$old
AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  chmod 600 "$STORE_DIR/rollback"
  log "upgrade done: CT $new is live, CT $old is stopped (rollback: $0 rollback --ctid $old)"
}

cmd_rollback() {
  local old="${CTID:-}"
  local rb="$STORE_DIR/rollback"
  [ -f "$rb" ] || die "no rollback record ($rb)"
  # shellcheck disable=SC1090
  . "$rb"
  [ -n "$old" ] || old="$OLD"
  [ "$old" = "$OLD" ] || die "rollback record is for CT $OLD, not $old"
  log "rolling back: stop CT $NEW, start CT $OLD"
  pct stop "$NEW" 2>/dev/null || true
  pct set "$NEW" --onboot 0 >/dev/null 2>&1 || true
  pct start "$OLD"
  pct set "$OLD" --onboot 1 >/dev/null
  wait_health "$OLD" 8770 || die "CT $OLD did not become healthy"
  log "rollback done: CT $OLD is live again (CT $NEW is stopped; $0 destroy --ctid $NEW to remove it)"
}

cmd_status() {
  local ctid="${CTID:-}"
  if [ -z "$ctid" ]; then
    for f in "$STORE_DIR"/*.conf; do
      [ -f "$f" ] || continue
      ctid="$(basename "$f" .conf)"
      printf 'CT %s: %s\n' "$ctid" "$(pct status "$ctid" 2>/dev/null || echo missing)"
    done
    return 0
  fi
  pct status "$ctid" || true
  pct exec "$ctid" -- python -c \
    "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4))))" \
    2>/dev/null || true
  pct exec "$ctid" -- python -c \
    "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8780/app/health', timeout=4))))" \
    2>/dev/null || true
}

cmd_destroy() {
  local ctid="${CTID:-}" purge="${1:-}"
  [ -n "$ctid" ] || die "usage: $0 destroy --ctid <CT> [--purge]"
  pct stop "$ctid" 2>/dev/null || true
  if [ "$purge" = "--purge" ]; then
    pct destroy "$ctid" --purge
  else
    pct destroy "$ctid"
  fi
  rm -f "$(record_file "$ctid")" "$(env_file "$ctid")"
  log "CT $ctid destroyed; the shared data dirs ${DATA_BASE}/{data,blobs,web} are kept"
}

# ---------------------------------------------------------------------------
# argument parsing: one command, then flags/KEY=VALUE
# ---------------------------------------------------------------------------

CMD="${1:-}"; shift || true
[ -n "$CMD" ] || usage 1
EXTRA_ENV=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ct) BUILD_CT="$2"; shift 2 ;;
    --ctid) CTID="$2"; shift 2 ;;
    --src) SRC="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --browser) BROWSER=1; shift ;;
    --sync) BROWSER=0; shift ;;
    --memory) MEMORY_BROWSER="$2"; MEMORY_SYNC="$2"; shift 2 ;;
    --cores) CORES_BROWSER="$2"; CORES_SYNC="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --rootfs-size) ROOTFS_SIZE="$2"; shift 2 ;;
    --data) DATA_BASE="$2"; shift 2 ;;
    --public-origin) EXTRA_ENV+=("STUDIO_WEB_PUBLIC_ORIGIN=$2"); shift 2 ;;
    --env) EXTRA_ENV+=("$2"); shift 2 ;;
    --no-onboot) ONBOOT=0; shift ;;
    --purge) DESTROY_PURGE=--purge; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
BROWSER="${BROWSER:-0}"

case "$CMD" in
  build)    cmd_build ;;
  export)   cmd_export ;;
  fetch)    cmd_fetch ;;
  create)   cmd_create ;;
  upgrade)  cmd_upgrade ;;
  rollback) cmd_rollback ;;
  status)   cmd_status ;;
  destroy)  cmd_destroy "${DESTROY_PURGE:-}" ;;
  help|-h|--help) usage 0 ;;
  *) usage 1 ;;
esac
