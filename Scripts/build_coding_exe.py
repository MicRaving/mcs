"""Build the portable CodingStudio desktop app.

Default = **onedir** (recommended): ``dist/<name>/<name>.exe`` + ``_internal/``.
There is no runtime self-extraction, so an antivirus scanner or a cloud-synced
folder cannot corrupt startup (the legacy onefile build failed with
``Failed to extract PIL\\avif...: decompression resulted in return code -3``).
Pass ``--onefile`` (or set ``CODINGSTUDIO_ONEFILE=1``) for the legacy single file.

``--name NAME`` (or ``CODINGSTUDIO_NAME``) sets the distribution's name.  It is
how a branch builds *beside* an installed app instead of over it — the finished
folder is ``<name>/`` with ``<name>.exe`` in it, and the old
``CodingStudio/`` is left alone.

The finished distribution is also copied to the repo root (``CodingStudio/`` for
onedir, ``CodingStudio.exe`` for onefile) so it can be launched beside ``Papers/``.

Run:  python Scripts/build_coding_exe.py [--lite] [--onefile] [--name NAME] [--version]
Out:  Scripts/coding_web/dist/<name>/  (and a copy at the repo root)
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_DIR = os.path.join(HERE, "coding_web")
ENTRY = os.path.join(APP_DIR, "app.py")
STATIC = os.path.join(APP_DIR, "static")
SCHEMA = os.path.join(APP_DIR, "schema.json")
DIST = os.path.join(APP_DIR, "dist")
BUILD_TMP = os.path.join(ROOT, "temp", "coding_web_build")
NAME = "CodingStudio"


def _app_version():
    try:
        sys.path.insert(0, APP_DIR)
        import studio_lib

        return getattr(studio_lib, "APP_VERSION", "unknown")
    except Exception:
        return "unknown"


def _remove(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        return True
    except OSError:
        return False


def _name_arg(argv):
    """The requested distribution name: --name NAME, else $CODINGSTUDIO_NAME."""
    for i, arg in enumerate(argv):
        if arg == "--name" and i + 1 < len(argv):
            return argv[i + 1].strip()
        if arg.startswith("--name="):
            return arg.split("=", 1)[1].strip()
    return (os.environ.get("CODINGSTUDIO_NAME") or "").strip()


def main():
    lite = "--lite" in sys.argv
    onefile = "--onefile" in sys.argv or os.environ.get("CODINGSTUDIO_ONEFILE") == "1"
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"CodingStudio source version: {_app_version()}")
        return 0
    name = _name_arg(sys.argv) or ("CodingStudioLite" if lite else NAME)
    if not os.path.exists(SCHEMA):
        print("schema.json missing - running build_coding_schema.py first")
        subprocess.check_call([sys.executable, os.path.join(HERE, "build_coding_schema.py")])

    work = os.path.abspath(BUILD_TMP)
    os.makedirs(work, exist_ok=True)

    # Clean any previous output so a locked/stale copy cannot be reported as fresh.
    for stale in (os.path.join(DIST, name), os.path.join(DIST, name + ".exe")):
        if os.path.exists(stale) and not _remove(stale):
            print(f"cannot replace {stale} - close the running app first, then rebuild")
            return 1

    spec = os.path.join(APP_DIR, "CodingStudio.spec")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", DIST,
        "--workpath", work,
        spec,
    ]
    print(" ".join(cmd))
    print(f"version: {_app_version()}  |  mode: {'onefile' if onefile else 'onedir'}")
    env = dict(os.environ)
    env["CODINGSTUDIO_NAME"] = name
    if lite:
        env["CODINGSTUDIO_LITE"] = "1"
        print("LITE build: no PyMuPDF (text falls back to Text/<Key>.txt)")
    if onefile:
        env["CODINGSTUDIO_ONEFILE"] = "1"
    print(f"name: {name}")
    subprocess.check_call(cmd, cwd=APP_DIR, env=env)

    if onefile:
        built = os.path.join(DIST, name + ".exe" if os.name == "nt" else name)
    else:
        built = os.path.join(DIST, name, name + ".exe" if os.name == "nt" else name)
    if not os.path.exists(built):
        print("build finished but no binary found - check the PyInstaller output above")
        return 1

    # Copy the finished distribution to the repo root (launch beside Papers/).
    if onefile:
        dest = os.path.join(ROOT, name + (".exe" if os.name == "nt" else ""))
        shutil.copy2(built, dest)
    else:
        dest_dir = os.path.join(ROOT, name)
        _remove(dest_dir)
        shutil.copytree(os.path.join(DIST, name), dest_dir)
        dest = os.path.join(dest_dir, name + ".exe" if os.name == "nt" else name)
        # A bare CodingStudio.exe at the repo root is a stray copy of the onedir
        # launcher: it looks for <root>/_internal and dies with "Failed to load
        # Python DLL ... python311.dll".  Warn (never delete) so it is obvious.
        stray = os.path.join(ROOT, name + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(stray):
            print(f"\n! stray launcher with no _internal beside it: {stray}")
            print(f"  run {os.path.relpath(dest, ROOT)} instead (or delete the stray copy).")

    if onefile:
        size = os.path.getsize(dest) / (1024 * 1024)
        print(f"\nbuilt {dest}  ({size:.1f} MB)")
        print("Double-click it (or drop it beside the folder containing Papers/ and run it).")
    else:
        total = sum(os.path.getsize(os.path.join(dp, f))
                    for dp, _dn, fn in os.walk(dest_dir) for f in fn)
        print(f"\nbuilt {dest}  (distribution {total / (1024 * 1024):.1f} MB, {os.path.relpath(dest_dir, ROOT)}/)")
        print("Run CodingStudio.exe from that folder (keep _internal/ beside it).")
    print("Options: --root DIR  --port N  --browser  --no-window")
    print("Diagnostics: <data root>/temp/CodingStudio.log")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
