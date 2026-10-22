"""In-app updates, adapted from the updater of the qcnext editor.

qcnext's updater is a Tauri shell plus signed delta hotpatches.  This app is a
PyInstaller bundle, so the shape is the same and the mechanism is simpler:

    check    -> the latest GitHub release of the repository
    download -> the asset for this platform, into temp/updates/, sha256-checked
    apply    -> unzip it, write a small swap script, quit; the script replaces
                the application folder and starts the new build

The version rules are qcnext's: ``X.Y.Z`` with an optional ``_NNN`` nightly
counter (``0.2.1_003`` sorts after ``0.2.1``), a ``v`` prefix is accepted, and
only a strictly newer release is offered.  There is one channel for now
(stable); the field is in the state so a nightly channel can be added without
touching the client.

Nothing here writes into a project: the download goes to the app's own temp
folder, and the swap only ever touches the application folder it runs from.
"""

import hashlib
import json
import os
import platform as _platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

from studio_lib import APP_VERSION
from studio_state import FROZEN, S, log

#: The repository the releases come from.
REPO = "MicRaving/mcs"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"
LATEST_URL = RELEASES_URL + "/latest"
PAGE_URL = f"https://github.com/{REPO}/releases/latest"

#: A check is reused for this long, and refreshed at most this often.
CHECK_TTL = 10 * 60
#: The automatic check on boot runs at most once a day (qcnext's "daily" cadence).
AUTO_CHECK_EVERY = 24 * 60 * 60
#: Refuse absurd downloads (a release asset is ~150-180 MB).
MAX_ASSET_BYTES = 600 * 1024 * 1024

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:_(\d{1,5}))?$")

_STATE = {
    "checked_at": 0.0,
    "result": None,          # the last successful check
    "error": "",
    "checking": False,
}
_LOCK = threading.Lock()

_DOWNLOAD = {
    "status": "idle",        # idle | downloading | ready | error
    "received": 0,
    "total": 0,
    "path": "",
    "error": "",
}
_DL_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #

def parse_version(text):
    """``X.Y.Z`` or ``X.Y.Z_NNN`` (with an optional ``v``) -> tuple, or None."""
    match = _VERSION_RE.match(str(text or "").strip())
    if not match:
        return None
    major, minor, patch, nightly = match.groups()
    return int(major), int(minor), int(patch), (int(nightly) if nightly else None)


def compare_versions(left, right):
    """-1 / 0 / +1, with a nightly of the same base sorting after its stable."""
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return 0
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    if a[3] == b[3]:
        return 0
    if a[3] is None:
        return -1
    if b[3] is None:
        return 1
    return -1 if a[3] < b[3] else 1


def is_newer(candidate, current):
    """Whether ``candidate`` is strictly newer than ``current``."""
    return compare_versions(candidate, current) > 0


# --------------------------------------------------------------------------- #
# the platform
# --------------------------------------------------------------------------- #

def platform_tag():
    """The asset suffix this build can install: ``windows-x64``, ``macos-arm64``…"""
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        machine = _platform.machine().lower()
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    return "linux-x64"


def in_flatpak():
    return bool(os.environ.get("FLATPAK_ID"))


def can_self_update():
    """Only a frozen build that is not managed by Flatpak can replace itself."""
    if in_flatpak():
        return False
    return bool(FROZEN)


def _asset_name(tag):
    """The release asset that belongs to this platform."""
    return f"CodingStudioUni-{tag}." + ("flatpak" if tag.startswith("linux") else "zip")


# --------------------------------------------------------------------------- #
# talking to GitHub
# --------------------------------------------------------------------------- #

def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"CodingStudio/{APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _release_summary(release):
    """The parts of a GitHub release this app uses."""
    tag = str(release.get("tag_name") or "").strip()
    assets = release.get("assets") or []
    tag_name = platform_tag()
    wanted = _asset_name(tag_name)
    asset = next((a for a in assets if a.get("name") == wanted), None)
    digest = ""
    if asset:
        digest = str(asset.get("digest") or "")
    return {
        "version": tag,
        "name": str(release.get("name") or tag),
        "page_url": str(release.get("html_url") or PAGE_URL),
        "published_at": str(release.get("published_at") or ""),
        "notes": str(release.get("body") or ""),
        "prerelease": bool(release.get("prerelease")),
        "platform": tag_name,
        "asset": ({
            "name": asset.get("name"),
            "size": int(asset.get("size") or 0),
            "url": asset.get("browser_download_url"),
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
        } if asset else None),
    }


def check(force=False):
    """Look for a newer release.  Returns the state the view renders."""
    with _LOCK:
        fresh = time.time() - float(_STATE["checked_at"] or 0) < CHECK_TTL
        if not force and fresh and _STATE["result"] is not None:
            return state()
        if _STATE["checking"]:
            return state()
        _STATE["checking"] = True
    try:
        release = _release_summary(_get_json(LATEST_URL))
        with _LOCK:
            _STATE["result"] = release
            _STATE["checked_at"] = time.time()
            _STATE["error"] = ""
    except urllib.error.HTTPError as exc:
        message = ("no release has been published yet"
                   if exc.code == 404 else f"GitHub answered {exc.code}")
        with _LOCK:
            _STATE["error"] = message
            _STATE["checked_at"] = time.time()
    except Exception as exc:  # noqa: BLE001 - offline is normal, never fatal
        with _LOCK:
            _STATE["error"] = f"could not reach GitHub ({exc.__class__.__name__})"
            _STATE["checked_at"] = time.time()
    finally:
        with _LOCK:
            _STATE["checking"] = False
    return state()


def _maybe_auto_check():
    """One check a day, in the background, the first time the view asks."""
    if time.time() - float(_STATE["checked_at"] or 0) < AUTO_CHECK_EVERY:
        return
    if _STATE["checking"]:
        return
    threading.Thread(target=check, args=(True,), daemon=True).start()


def state():
    """Everything the settings view needs to draw the update section."""
    with _LOCK:
        result = _STATE["result"]
        error = _STATE["error"]
        checking = _STATE["checking"]
        checked_at = _STATE["checked_at"]
    with _DL_LOCK:
        download = dict(_DOWNLOAD)

    latest = (result or {}).get("version") or ""
    available = bool(result and is_newer(latest, APP_VERSION))
    asset = (result or {}).get("asset") if available else None
    if in_flatpak():
        note = ("This copy is installed as a Flatpak. Update it with "
                "`flatpak update` — the bundle is replaced by the system, not by the app.")
    elif not FROZEN:
        note = ("This is a source run, so the app cannot replace itself. "
                "Download the release for your platform instead.")
    else:
        note = ""

    return {
        "current": APP_VERSION,
        "latest": latest,
        "available": available,
        "checked_at": checked_at,
        "checking": checking,
        "error": error,
        "frozen": bool(FROZEN),
        "platform": platform_tag(),
        "can_self_update": can_self_update(),
        "note": note,
        "page_url": (result or {}).get("page_url") or PAGE_URL,
        "notes": (result or {}).get("notes") or "",
        "published_at": (result or {}).get("published_at") or "",
        "asset": asset,
        "download": download,
    }


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #

def _updates_dir():
    path = os.path.join(S.TEMP, "updates")
    os.makedirs(path, exist_ok=True)
    return path


def download_async():
    """Start the download in a thread (the view polls ``state``)."""
    with _LOCK:
        result = _STATE["result"]
    if not result:
        return {"ok": False, "error": "check for updates first"}
    asset = (result or {}).get("asset")
    if not asset or not asset.get("url"):
        return {"ok": False, "error": f"the release has no {platform_tag()} build"}
    with _DL_LOCK:
        if _DOWNLOAD["status"] == "downloading":
            return {"ok": True, "started": False}
        _DOWNLOAD.update({"status": "downloading", "received": 0,
                          "total": int(asset.get("size") or 0), "path": "",
                          "error": ""})
    threading.Thread(target=_download, args=(asset,), daemon=True).start()
    return {"ok": True, "started": True}


def _download(asset):
    target = os.path.join(_updates_dir(), os.path.basename(asset["name"]))
    part = target + ".part"
    digest = hashlib.sha256()
    received = 0
    try:
        req = urllib.request.Request(asset["url"], headers={
            "User-Agent": f"CodingStudio/{APP_VERSION}",
        })
        with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or asset.get("size") or 0)
            with _DL_LOCK:
                _DOWNLOAD["total"] = total
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_ASSET_BYTES:
                    raise ValueError("the download is larger than any release asset")
                digest.update(chunk)
                fh.write(chunk)
                with _DL_LOCK:
                    _DOWNLOAD["received"] = received
        expected = (asset.get("sha256") or "").lower()
        if expected and digest.hexdigest().lower() != expected:
            raise ValueError("the downloaded file does not match the release checksum")
        os.replace(part, target)
        with _DL_LOCK:
            _DOWNLOAD.update({"status": "ready", "received": received,
                              "total": received or _DOWNLOAD["total"], "path": target})
    except Exception as exc:  # noqa: BLE001
        log(f"update download failed: {exc}")
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        with _DL_LOCK:
            _DOWNLOAD.update({"status": "error", "error": str(exc)})


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #

def _app_target():
    """The path the update replaces, and the executable to start afterwards.

    Windows/Linux onedir: the folder holding the executable.  macOS: the
    ``.app`` bundle around ``Contents/MacOS``.
    """
    from studio_state import EXE_DIR

    if sys.platform == "darwin":
        contents = os.path.dirname(EXE_DIR)              # .../Contents
        bundle = os.path.dirname(contents)               # .../X.app
        if bundle.endswith(".app"):
            return bundle, bundle
        return EXE_DIR, os.path.join(EXE_DIR, "CodingStudioUni")
    exe = os.path.join(EXE_DIR, "CodingStudioUni" + (".exe" if sys.platform == "win32" else ""))
    return EXE_DIR, exe


def apply():
    """Unzip the download, write the swap script and quit into it.

    Returns ``{"ok": bool, "message": str, "restarting": bool}``.  Nothing is
    replaced by this process: a small script waits for the app to exit, swaps
    the folder and starts the new build, so a running executable is never
    overwritten on disk.
    """
    if not can_self_update():
        return {"ok": False, "restarting": False,
                "message": state()["note"] or "this copy cannot update itself"}
    with _DL_LOCK:
        download = dict(_DOWNLOAD)
    if download["status"] != "ready" or not download["path"]:
        return {"ok": False, "restarting": False, "message": "nothing has been downloaded"}

    staging = os.path.join(_updates_dir(), "staged")
    try:
        if os.path.isdir(staging):
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        with zipfile.ZipFile(download["path"]) as zf:
            for member in zf.namelist():
                # refuse absolute paths and traversal: a release asset is
                # downloaded from the network and must never write outside staging
                target = os.path.normpath(os.path.join(staging, member))
                if not target.startswith(os.path.normpath(staging) + os.sep):
                    raise ValueError(f"the archive contains an unsafe path: {member}")
            zf.extractall(staging)
    except Exception as exc:  # noqa: BLE001
        log(f"update staging failed: {exc}")
        return {"ok": False, "restarting": False, "message": f"the download could not be unpacked: {exc}"}

    new_dir = os.path.join(staging, "CodingStudioUni")
    if not os.path.isdir(new_dir):
        # the macOS zip carries the .app bundle directly
        candidates = [os.path.join(staging, n) for n in os.listdir(staging)]
        new_dir = next((c for c in candidates if c.endswith(".app")), "")
        if not new_dir:
            return {"ok": False, "restarting": False,
                    "message": "the archive does not contain the application"}

    target, exe = _app_target()
    script = _write_swap_script(target, new_dir, exe)
    if not script:
        return {"ok": False, "restarting": False, "message": "the swap script could not be written"}
    _start_detached(script)
    threading.Timer(0.6, _quit).start()
    return {"ok": True, "restarting": True,
            "message": "the new version is being installed; the app will restart"}


def _write_swap_script(target, new_dir, exe):
    """A script that waits for this process, swaps the folder and relaunches."""
    folder = _updates_dir()
    if sys.platform == "win32":
        path = os.path.join(folder, "apply-update.cmd")
        name = os.path.basename(exe)
        body = "\r\n".join([
            "@echo off",
            "setlocal",
            f'set "NEW={new_dir}"',
            f'set "APP={target}"',
            'set "OLD=%APP%.old"',
            "ping -n 3 127.0.0.1 >nul",
            ":wait",
            f'tasklist /FI "IMAGENAME eq {name}" 2>nul | find /I "{name}" >nul',
            "if not errorlevel 1 (",
            "  ping -n 2 127.0.0.1 >nul",
            "  goto wait",
            ")",
            'if exist "%OLD%" rmdir /s /q "%OLD%" 2>nul',
            'move "%APP%" "%OLD%" >nul',
            'move "%NEW%" "%APP%" >nul',
            f'start "" "%APP%\\{name}"',
            'rmdir /s /q "%OLD%" 2>nul',
            "",
        ])
    else:
        path = os.path.join(folder, "apply-update.sh")
        body = "\n".join([
            "#!/bin/sh",
            "sleep 2",
            f'while pgrep -f "{os.path.basename(exe)}" >/dev/null 2>&1; do sleep 1; done',
            f'APP="{target}"',
            f'NEW="{new_dir}"',
            'OLD="$APP.old"',
            'rm -rf "$OLD"',
            'mv "$APP" "$OLD" || exit 1',
            'mv "$NEW" "$APP" || { mv "$OLD" "$APP"; exit 1; }',
            'if [ -d "$APP" ]; then open "$APP"; fi',
            'rm -rf "$OLD"',
            "",
        ])
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        if sys.platform != "win32":
            os.chmod(path, 0o755)
        return path
    except OSError as exc:
        log(f"update swap script could not be written: {exc}")
        return ""


def _start_detached(script):
    """Start the swap script so it outlives this process."""
    try:
        if sys.platform == "win32":
            flags = 0
            for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
                flags |= getattr(subprocess, name, 0)
            subprocess.Popen(["cmd", "/c", script], close_fds=True,
                             creationflags=flags, cwd=os.path.dirname(script))
        else:
            subprocess.Popen(["/bin/sh", script], start_new_session=True,
                             close_fds=True, cwd=os.path.dirname(script))
    except Exception as exc:  # noqa: BLE001
        log(f"the update script could not be started: {exc}")


def _quit():
    """Exit so the script can replace the folder."""
    log("quitting for an update")
    os._exit(0)


def reset_for_tests():
    """Forget the cached check and download (used by the tests)."""
    with _LOCK:
        _STATE.update({"checked_at": 0.0, "result": None, "error": "", "checking": False})
    with _DL_LOCK:
        _DOWNLOAD.update({"status": "idle", "received": 0, "total": 0,
                          "path": "", "error": ""})
