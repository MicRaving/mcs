"""Paper-coding web app for the misinformation-intervention meta-analysis.

Serves a three-pane coder (paper list + rationale | schema form | paper viewer)
and saves each coder's schema-compatible JSON with a coder suffix, never touching
the canonical ``Extraction/<Key>.json``.

Run from source:  python Scripts/coding_web/app.py [--port 8777] [--root DIR] [--open]
Portable build:   python Scripts/build_coding_exe.py
"""

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

from flask import Flask

from studio_state import S, FROZEN, configure, log, refresh_keys
from studio_lib import APP_VERSION

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False
# Flask 2.3 moved the switch to the JSON provider, and the provider sorts
# the keys by default.  The app wants the keys in the order it built them
# (a criterion's auto-approval rules are a dictionary whose order is the
# insertion order), so the switch is set on the provider as well.
try:
    app.json.sort_keys = False
except AttributeError:  # pragma: no cover - Flask < 2.2
    pass

# The route modules only build their blueprints; they are registered here so the
# import graph stays acyclic (route modules never import app.py back).
from routes_core import bp as core_bp            # noqa: E402
from routes_scheme import bp as scheme_bp        # noqa: E402
from routes_criteria import bp as criteria_bp    # noqa: E402
from routes_project import bp as project_bp      # noqa: E402
from routes_harmonizer import bp as harmonizer_bp  # noqa: E402
from routes_coverage import bp as coverage_bp    # noqa: E402
from routes_summary import bp as summary_bp      # noqa: E402
from routes_screening import bp as screening_bp  # noqa: E402
from routes_agreement import bp as agreement_bp  # noqa: E402
from harmonizer_agreement import bp as harm_agreement_bp  # noqa: E402
from routes_analysis import bp as analysis_bp    # noqa: E402
from routes_apa import bp as apa_bp              # noqa: E402
from routes_fulltext import bp as fulltext_bp    # noqa: E402
from routes_repro import bp as repro_bp          # noqa: E402
from routes_settings import bp as settings_bp      # noqa: E402

for _bp in (core_bp, scheme_bp, criteria_bp, project_bp, harmonizer_bp,
            coverage_bp, summary_bp, screening_bp, agreement_bp,
            harm_agreement_bp, analysis_bp, apa_bp, fulltext_bp, repro_bp,
            settings_bp):
    app.register_blueprint(_bp)

# The corpus-wide GETs are answered from a file-stamp cache: they are pure
# functions of Extraction/, Codings/ and Screening/, so they are recomputed
# only when one of those files changes.
from studio_cache import register_response_cache  # noqa: E402

register_response_cache(app)



def free_port(preferred):
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _safe_stream(name):
    """A writable text stream for ``sys.stdout``/``sys.stderr``.

    A windowed (console-less) PyInstaller build can hand us a broken stderr
    handle; writing to it raises ``OSError`` and takes the server down before it
    ever starts.  Probe the real stream and fall back to the null device.
    """
    stream = getattr(sys, name, None)
    try:
        if stream is not None:
            stream.write("")
            stream.flush()
            return stream
    except Exception:
        pass
    try:
        return open(os.devnull, "w", encoding="utf-8")
    except OSError:
        return stream


def _ensure_std_streams():
    sys.stdout = _safe_stream("stdout")
    sys.stderr = _safe_stream("stderr")


def serve(host, port):
    logging.getLogger("werkzeug").disabled = True
    app.logger.disabled = True
    # Warm the analysis caches (moderator scan, spec tables, figure index) and
    # regenerate any stale tables in the background while the first page loads,
    # so the Analysis tab is ready when the user opens it.
    try:
        from analysis_lib import _prewarm_analysis, autorun_if_stale

        _prewarm_analysis()
        autorun_if_stale()
    except Exception:  # noqa: BLE001
        pass
    # ``Flask.run`` prints a startup banner to stderr, which crashes in a
    # windowed build; ``make_server`` serves quietly instead.
    try:
        from werkzeug.serving import make_server

        srv = make_server(host, port, app, threaded=True)
    except Exception as exc:  # noqa: BLE001
        log(f"make_server failed, falling back to app.run: {exc!r}")
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        return
    srv.serve_forever()


class DesktopApi:
    """Exposed to the page as ``window.pywebview.api``.

    pywebview runs these on its GUI thread, which is the only place a native
    file dialog may be opened from.
    """

    def choose_folder(self):
        try:
            import webview

            windows = list(getattr(webview, "windows", []) or [])
            if not windows:
                return ""
            result = windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if not result:
                return ""
            return str(result[0] if isinstance(result, (list, tuple)) else result)
        except BaseException:  # noqa: BLE001
            import traceback

            log("choose_folder failed:\n" + traceback.format_exc())
            return ""

    def choose_file(self):
        """A native open-file dialog for a JSON file (the Scheme tab's
        "Load scheme"); ``""`` when cancelled or unavailable."""
        try:
            import webview

            windows = list(getattr(webview, "windows", []) or [])
            if not windows:
                return ""
            # pywebview >= 5 names it FileDialog.OPEN; older builds OPEN_DIALOG
            mode = getattr(getattr(webview, "FileDialog", None), "OPEN", None)
            if mode is None:
                mode = webview.OPEN_DIALOG
            result = windows[0].create_file_dialog(
                mode, allow_multiple=False,
                file_types=("JSON files (*.json)", "All files (*.*)"))
            if not result:
                return ""
            return str(result[0] if isinstance(result, (list, tuple)) else result)
        except BaseException:  # noqa: BLE001
            import traceback

            log("choose_file failed:\n" + traceback.format_exc())
            return ""

    def current_root(self):
        return S.ROOT


#: The folders of the bundle that hold .NET assemblies (pythonnet's runtime,
#: clr_loader's host shim and the WebView2 wrappers pywebview loads).
_MANAGED_DIRS = (
    os.path.join("pythonnet", "runtime"),
    os.path.join("clr_loader", "ffi", "dlls"),
    os.path.join("webview", "lib"),
)


def _unblock_bundle():
    """Remove the Mark of the Web from the bundle's .NET assemblies (Windows).

    A zip downloaded with a browser and extracted with Explorer marks every
    file it contains as coming from the internet (an NTFS ``Zone.Identifier``
    stream).  .NET Framework refuses to load such an assembly into the default
    domain, so ``import clr`` dies with "Failed to resolve
    Python.Runtime.Loader.Initialize", pywebview finds no WinForms backend and
    the app ends up in the browser.  A locally built copy and the installer
    carry no mark, which is why only the downloaded zip showed it.  Deleting
    the stream is what ``Unblock-File`` does; it only ever touches the app's
    own files, and a read-only install (Program Files) carries no mark anyway.
    Returns the number of files unblocked.
    """
    if not (FROZEN and sys.platform == "win32"):
        return 0
    base = getattr(sys, "_MEIPASS", "") or os.path.dirname(os.path.abspath(sys.executable))
    count = 0
    for rel in _MANAGED_DIRS:
        for dirpath, _dirs, files in os.walk(os.path.join(base, rel)):
            for name in files:
                if not name.lower().endswith(".dll"):
                    continue
                try:
                    os.remove(os.path.join(dirpath, name) + ":Zone.Identifier")
                    count += 1
                except OSError:
                    pass  # no mark (the normal case) or not ours to change
    return count


def _dist_version(name):
    """The installed version of a distribution (``pywebview``, ``pythonnet``)."""
    try:
        from importlib import metadata

        return metadata.version(name)
    except Exception:  # noqa: BLE001 - the metadata is optional in a bundle
        return "?"


def _webview_backend():
    """The GUI backend pywebview picked (``winforms/edgechromium``, ``cocoa``…)."""
    try:
        from webview import guilib

        lib = getattr(guilib, "guilib", None)
        if lib is None:
            return "none"
        name = lib.__name__.rsplit(".", 1)[-1]
        renderer = getattr(lib, "renderer", "")
        return f"{name}/{renderer}" if renderer else name
    except Exception:  # noqa: BLE001
        return "?"


def run_app(host, port, url, mode="native"):
    """mode: native window (pywebview) | browser tab | none (plain server)."""
    if mode == "none":
        serve(host, port)
        return
    threading.Thread(target=serve, args=(host, port), daemon=True).start()
    if mode == "native":
        unblocked = _unblock_bundle()
        if unblocked:
            log(f"native window: removed the download mark from {unblocked} bundled assemblies")
        wanted = {"win32": "winforms (pythonnet " + _dist_version("pythonnet") + ")",
                  "darwin": "cocoa (pyobjc " + _dist_version("pyobjc-core") + ")"}.get(
                      sys.platform, "gtk/qt")
        try:
            import webview

            log(f"native window: pywebview {_dist_version('pywebview')}, "
                f"trying {wanted}")
            webview.create_window(
                "Coding Studio", url, width=1460, height=940, min_size=(880, 600),
                js_api=DesktopApi(),
            )
            # ``func`` runs once the GUI loop is up: the moment the backend is known
            webview.start(func=lambda: log(f"native window open (backend {_webview_backend()})"))
            log("native window closed")
            return
        except BaseException:  # noqa: BLE001 — SystemExit from the GUI layer included
            import traceback

            log(f"native window failed (tried {wanted}, backend "
                f"{_webview_backend()}):\n" + traceback.format_exc())
    log("falling back to the default browser")
    try:
        webbrowser.open(url)
    except Exception:
        log("could not open a browser")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    _ensure_std_streams()
    parser = argparse.ArgumentParser(description="Paper-coding desktop app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777, help="preferred port (auto-picked if busy)")
    parser.add_argument("--root", default=None, help="folder containing Papers/, Text/, Extraction/")
    parser.add_argument("--native", action="store_true", help="open in a native app window (default)")
    parser.add_argument("--open", action="store_true",
                        help="open the app window/browser (the default); accepted for compatibility")
    parser.add_argument("--browser", action="store_true", help="open in the default browser instead")
    parser.add_argument("--no-window", action="store_true", help="serve only, no window (headless)")
    parser.add_argument("--version", action="store_true", help="print the build version and exit")
    args = parser.parse_args()

    if args.version:
        print(f"CodingStudio {APP_VERSION} (frozen={FROZEN})")
        return

    configure(args.root)
    refresh_keys()

    port = free_port(args.port)
    url = f"http://{args.host}:{port}"
    mode = "none" if args.no_window else ("browser" if args.browser else "native")
    log(f"start mode={mode} root={S.ROOT} papers={len(S.KEYS)} url={url} frozen={FROZEN}")
    print("Coding Studio")
    print(f"  data root : {S.ROOT}")
    print(f"  codings   : {os.path.relpath(S.CODINGS, S.ROOT)}")
    print(f"  papers    : {len(S.KEYS)} found")
    print(f"  open at   : {url}")
    if not os.path.isdir(os.path.join(S.ROOT, "Papers")):
        print("  ! no Papers/ folder next to the app or above it — pass --root DIR")
    run_app(args.host, port, url, mode)


if __name__ == "__main__":
    main()
