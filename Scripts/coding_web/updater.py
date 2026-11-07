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


def installed_with_setup():
    """Whether this copy was installed by the Windows (Inno Setup) installer.

    The installer leaves its uninstaller (``unins000.exe``) beside the exe.
    Such a copy usually sits under Program Files, where the folder swap would
    need admin rights, and the uninstaller keeps a file list the swap would
    invalidate - so it is updated by running the next installer instead.
    """
    if sys.platform != "win32" or not FROZEN:
        return False
    from studio_state import EXE_DIR

    try:
        return any(re.match(r"^unins\d{3}\.exe$", name, re.I)
                   for name in os.listdir(EXE_DIR))
    except OSError:
        return False


def can_self_update():
    """Only a frozen Windows/macOS build can replace itself.

    A Flatpak is updated by the system, and Linux has no zip release to swap in
    (the only Linux asset is the ``.flatpak``).
    """
    if in_flatpak() or not FROZEN:
        return False
    return sys.platform in ("win32", "darwin")


def _asset_matches(name, tag, installer=False):
    """Whether the release asset ``name`` belongs to this platform.

    The portable zips are ``CodingStudioUni-<tag>.zip``; the Windows installer
    carries the version in its name (``CodingStudioUni-0.2.1-windows-x64-setup.exe``).
    On macOS the zip is used even though a ``.dmg`` exists: the zip can be
    unpacked without mounting anything.
    """
    name = str(name or "")
    if installer:
        return bool(re.match(rf"^CodingStudioUni-.+-{re.escape(tag)}-setup\.exe$", name, re.I))
    ext = "flatpak" if tag.startswith("linux") else "zip"
    return name == f"CodingStudioUni-{tag}.{ext}"


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
    installer = installed_with_setup()
    asset = next((a for a in assets
                  if _asset_matches(a.get("name"), tag_name, installer)), None)
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
        "installer": installer,
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
    elif not can_self_update():
        note = ("This build cannot replace itself. "
                "Download the release for your platform instead.")
    elif installed_with_setup():
        note = ("This copy was installed with the Windows installer: the update "
                "runs the new installer, which may ask for administrator rights.")
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
        "installer": installed_with_setup(),
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
    """The per-user download folder - never the project, never the app folder.

    It used to be ``<project>/temp/updates``.  Without a project the project
    *is* the app folder, so the staged build sat inside the folder the swap
    moves away (and the move of the staged build then failed); with a project
    the download landed in a research folder that is often cloud-synced.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "CodingStudio", "updates")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
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

    Windows onedir: the folder holding the executable.  macOS: the ``.app``
    bundle around ``Contents/MacOS``.
    """
    from studio_state import EXE_DIR

    if sys.platform == "darwin":
        contents = os.path.dirname(EXE_DIR)              # .../Contents
        bundle = os.path.dirname(contents)               # .../X.app
        if bundle.endswith(".app"):
            return bundle, bundle
        return EXE_DIR, os.path.join(EXE_DIR, os.path.basename(sys.executable))
    # the running executable's own name, so a renamed build restarts itself
    return EXE_DIR, os.path.join(EXE_DIR, os.path.basename(sys.executable))


def _fail(message):
    log(f"update: {message}")
    return {"ok": False, "restarting": False, "message": message}


def _safe_extract(archive, staging):
    """Unpack ``archive`` into ``staging``, refusing anything outside it.

    macOS uses ``ditto``: the .app bundle holds symlinks and executable bits
    that :mod:`zipfile` drops (the swapped-in app would not start).
    """
    root = os.path.realpath(staging)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            # a release asset comes from the network: no absolute paths, no
            # drive letters, no ``..`` - nothing may land outside staging
            target = os.path.realpath(os.path.join(root, member))
            if os.path.isabs(member) or (target != root
                                         and not target.startswith(root + os.sep)):
                raise ValueError(f"the archive contains an unsafe path: {member}")
        if sys.platform != "darwin":
            zf.extractall(root)
            return
    subprocess.run(["/usr/bin/ditto", "-x", "-k", archive, root], check=True,
                   capture_output=True)


def apply():
    """Install the download: swap the app folder, or run the installer.

    Returns ``{"ok": bool, "message": str, "restarting": bool}``.  Nothing is
    replaced by this process: a small script waits for the app to exit, swaps
    the folder (or runs the Windows installer) and starts the new build, so a
    running executable is never overwritten on disk.
    """
    if not can_self_update():
        return {"ok": False, "restarting": False,
                "message": state()["note"] or "this copy cannot update itself"}
    with _DL_LOCK:
        download = dict(_DOWNLOAD)
    if download["status"] != "ready" or not download["path"]:
        return {"ok": False, "restarting": False, "message": "nothing has been downloaded"}

    target, exe = _app_target()
    if download["path"].lower().endswith(".exe"):
        # an installed copy: the next installer replaces it in place
        script = _write_installer_script(download["path"], exe)
    else:
        if installed_with_setup():
            return _fail("this copy was installed with the installer; "
                         "check for updates again to download the installer")
        if "/AppTranslocation/" in target:
            return _fail("macOS runs this copy from a read-only quarantine folder; "
                         "move CodingStudioUni.app to Applications and start it again")
        new_dir = _stage(download["path"], target)
        if isinstance(new_dir, dict):
            return new_dir
        script = _write_swap_script(target, new_dir, exe)
    if not script:
        return _fail("the update script could not be written")
    if not _start_detached(script):
        return _fail("the update script could not be started")
    threading.Timer(0.6, _quit).start()
    return {"ok": True, "restarting": True,
            "message": "the new version is being installed; the app will restart"}


def _stage(archive, target):
    """Unpack the zip and park the new build beside ``target``.

    The new build has to be on the same volume as the app: a directory
    ``move`` across volumes fails, and the download folder is often on
    another drive than a portable copy.  Returns the parked path, or a
    failure dict.
    """
    import shutil

    staging = os.path.join(_updates_dir(), "staged")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        _safe_extract(archive, staging)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"the download could not be unpacked: {exc}")

    new_dir = os.path.join(staging, os.path.basename(target))
    if not os.path.isdir(new_dir):
        candidates = [os.path.join(staging, n) for n in os.listdir(staging)]
        want = ".app" if sys.platform == "darwin" else ""
        new_dir = next((c for c in candidates
                        if os.path.isdir(c) and c.endswith(want)), "")
    if not new_dir or not os.path.isdir(new_dir):
        return _fail("the archive does not contain the application")

    parked = target + ".new"
    try:
        shutil.rmtree(parked, ignore_errors=True)
        shutil.move(new_dir, parked)
    except Exception as exc:  # noqa: BLE001 - e.g. no write access beside the app
        return _fail(f"the new version could not be placed beside the app ({exc}); "
                     "move the app to a folder you can write to, or install the new "
                     "release by hand")
    return parked


def _cmd_quote(path):
    """A path for a ``set "VAR=..."`` line of a batch file.

    Inside double quotes ``&``, ``^`` and spaces are literal; only ``%`` is
    still expanded in a batch file and must be doubled.
    """
    return str(path).replace("%", "%%")


def _cmd_wait_lines():
    """Batch lines that wait until this very process has exited.

    Waiting on the PID, not the image name, so another running copy of the app
    (or a second build beside it) cannot keep the update waiting forever.
    """
    pid = os.getpid()
    return [
        "ping -n 3 127.0.0.1 >nul",
        ":wait",
        f'tasklist /FI "PID eq {pid}" /NH 2>nul | find " {pid} " >nul',
        "if not errorlevel 1 (",
        "  ping -n 2 127.0.0.1 >nul",
        "  goto wait",
        ")",
    ]


def _write_installer_script(installer, exe):
    """Windows, installed copy: wait, run the installer silently, restart.

    ``start /wait`` goes through ShellExecute, so the installer's request for
    administrator rights shows the UAC prompt (a plain ``CreateProcess`` of an
    elevated program fails with error 740).
    """
    folder = _updates_dir()
    path = os.path.join(folder, "apply-update.cmd")
    body = "\r\n".join([
        "@echo off",
        "setlocal",
        f'set "SETUP={_cmd_quote(installer)}"',
        f'set "EXE={_cmd_quote(exe)}"',
        *_cmd_wait_lines(),
        'start "" /wait "%SETUP%" /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS',
        'if exist "%EXE%" start "" "%EXE%"',
        "",
    ])
    return _write_script(path, body)


def _write_swap_script(target, new_dir, exe):
    """A script that waits for this process, swaps the folder and relaunches.

    Every move is checked: when the old folder cannot be moved away (a file is
    still locked) the new build is left parked and the old one restarted,
    instead of the new folder being moved *into* the old one.
    """
    folder = _updates_dir()
    if sys.platform == "win32":
        path = os.path.join(folder, "apply-update.cmd")
        name = os.path.basename(exe)
        body = "\r\n".join([
            "@echo off",
            "setlocal",
            f'set "NEW={_cmd_quote(new_dir)}"',
            f'set "APP={_cmd_quote(target)}"',
            'set "OLD=%APP%.old"',
            *_cmd_wait_lines(),
            'if exist "%OLD%" rmdir /s /q "%OLD%" 2>nul',
            'move "%APP%" "%OLD%" >nul || goto restart',
            'move "%NEW%" "%APP%" >nul || (move "%OLD%" "%APP%" >nul & goto restart)',
            'rmdir /s /q "%OLD%" 2>nul',
            ":restart",
            f'start "" "%APP%\\{_cmd_quote(name)}"',
            "",
        ])
    else:
        import shlex

        path = os.path.join(folder, "apply-update.sh")
        body = "\n".join([
            "#!/bin/sh",
            "sleep 2",
            # the PID, not ``pgrep -f <name>``: the script's own command line
            # contains the app's name once the download folder is named after it
            f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 1; done",
            f"APP={shlex.quote(target)}",
            f"NEW={shlex.quote(new_dir)}",
            'OLD="$APP.old"',
            'rm -rf "$OLD"',
            'if mv "$APP" "$OLD"; then',
            '  mv "$NEW" "$APP" || mv "$OLD" "$APP"',
            "fi",
            'if [ -d "$APP" ]; then open "$APP"; else "$APP" & fi',
            'rm -rf "$OLD"',
            "",
        ])
    return _write_script(path, body)


def _write_script(path, body):
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        if sys.platform != "win32":
            os.chmod(path, 0o755)
        return path
    except OSError as exc:
        log(f"update script could not be written: {exc}")
        return ""


def _start_detached(script):
    """Start the update script so it outlives this process."""
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
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"the update script could not be started: {exc}")
        return False


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
