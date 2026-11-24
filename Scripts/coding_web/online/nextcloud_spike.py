"""Nextcloud WebDAV spike: does this share give us atomic conditional writes?

Answers the phase-0/phase-9 questions (PLAN §13, §17) with measurements instead
of assumptions, because "Nextcloud without server" promises *best-effort*
locking only where the server refuses to be trustworthy.

Usage (real server - the owner supplies the values)::

    set NC_URL=https://cloud.example.org
    set NC_USER=anna
    set NC_APP_PASSWORD=xxxxx-xxxxx-xxxxx
    set NC_FOLDER=CodingStudio-spike-2026-11
    python Scripts/coding_web/online/nextcloud_spike.py

    # or one of the local test servers (no credentials, no network):
    python Scripts/coding_web/online/nextcloud_spike.py --local
    python Scripts/coding_web/online/nextcloud_spike.py --local --wsgidav

What it measures
----------------
1. ``PUT If-None-Match: *`` on an **existing** file -> 412?  (atomic create)
2. ``PUT If-Match: <stale etag>`` -> 412?                      (lost-update guard)
3. does the **folder** ETag change when a child changes?       (cheap freshness)
4. does ``PROPFIND Depth: infinity`` work, and how long for N files?
5. is the share rate limiting (429/503 + ``Retry-After``) under a burst?
6. 8 concurrent ``If-None-Match: *`` creates on one path: exactly one 201?

Result: a JSON verdict at ``temp/nextcloud/spike_result.json`` and a short
report on stdout.  It **never** prints the password, the app password or the
user name, and it never touches anything outside ``NC_FOLDER``: that folder is
created by the script, everything it writes is relative to it, and it is
deleted again at the end (``--keep`` leaves it for inspection).

Nothing here decides anything by itself - the verdict is written down, and the
Nextcloud backend keeps ``guarantees: "best-effort"`` either way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone

_CODING_WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.dirname(_CODING_WEB)
for _p in (_SCRIPTS, _CODING_WEB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from online.errors import NotFoundError, RateLimitedError, ServerError  # noqa: E402
from online.webdav import PreconditionFailed, WebDAV, is_loopback  # noqa: E402

FILE_COUNT = 25
BURST = 40
RACE_THREADS = 8
RESULT_PATH = os.path.join(_SCRIPTS, "..", "temp", "nextcloud", "spike_result.json")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _host_only(url: str) -> str:
    """``https://cloud.example.org/remote.php/...`` -> ``https://cloud.example.org``."""
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme}://{u.hostname}{port}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _local_server(kind: str):
    """Start a throw-away WebDAV share for ``--local``; returns (server, root)."""
    root = tempfile.mkdtemp(prefix=f"nc-spike-{kind}-")
    if kind == "wsgidav":
        from online.tests.wsgidav_server import WsgiDAVServer, available
        if not available():
            raise SystemExit("wsgidav/cheroot are not installed in temp/nextcloud/venv")
        server = WsgiDAVServer(root)
    else:
        from online.tests.webdav_fake import WebDAVFake
        server = WebDAVFake(root)
    url = server.start()
    return server, root, url


# ---------------------------------------------------------------------------
# the measurements
# ---------------------------------------------------------------------------

def run(dav: WebDAV, folder_name: str, *, files: int = FILE_COUNT) -> dict:
    """Measure *dav* (pointed at the scratch folder) and return the verdict."""
    out: dict = {"started": _now(), "files": files}

    # 1 + 2: conditional create and stale If-Match -----------------------------
    probe = "probe.txt"
    dav.put(probe, b"one\n", if_none_match=True)
    etag1 = dav.stat(probe).etag
    out["etag_on_put"] = bool(etag1)          # is a strong etag returned at all?

    created_on_existing = None
    try:
        dav.put(probe, b"two\n", if_none_match=True)
        created_on_existing = True            # the server replaced it: NOT atomic
    except PreconditionFailed:
        created_on_existing = False
    out["if_none_match_star_on_existing_refused"] = created_on_existing is False

    dav.put(probe, b"three\n")                 # unconditional replace
    etag3 = dav.stat(probe).etag
    stale_refused = None
    try:
        dav.put(probe, b"four\n", if_match=etag1)
        stale_refused = False
    except PreconditionFailed:
        stale_refused = True
    out["stale_if_match_refused"] = bool(stale_refused)
    fresh_ok = None
    try:
        resp = dav.put(probe, b"five\n", if_match=etag3)
        fresh_ok = 200 <= resp.status < 300
    except PreconditionFailed:
        fresh_ok = False
    out["fresh_if_match_accepted"] = bool(fresh_ok)

    # 3: is the folder etag propagated up? -------------------------------------
    dav.makedirs("folder-probe")
    before = dav.stat("folder-probe").etag
    dav.put("folder-probe/child.txt", b"child\n")
    after = dav.stat("folder-probe").etag
    out["folder_etag"] = {"before": before, "after": after,
                          "propagates": bool(before and after and before != after)}
    root_before = dav.stat("").etag
    dav.put("folder-probe/child2.txt", b"child2\n")
    root_after = dav.stat("").etag
    out["root_folder_etag"] = {"before": root_before, "after": root_after,
                               "propagates": bool(root_before and root_after and root_before != root_after)}
    dav.delete("folder-probe")

    # 4: PROPFIND Depth: infinity -----------------------------------------------
    dav.makedirs("many")
    for i in range(files):
        dav.put(f"many/f{i:04d}.txt", f"file {i}\n".encode())
    started = time.perf_counter()
    try:
        rows = dav.propfind("", depth="infinity")
        seconds = time.perf_counter() - started
        seen = {p.path for p in rows if not p.is_dir}
        out["depth_infinity"] = {"supported": True, "seconds": round(seconds, 3),
                                 "returned": len(seen),
                                 "complete": len([p for p in seen if p.startswith("many/")]) == files}
    except Exception as exc:                  # noqa: BLE001 - any refusal is the answer
        seconds = time.perf_counter() - started
        out["depth_infinity"] = {"supported": False, "seconds": round(seconds, 3),
                                 "error": f"{type(exc).__name__}: {exc}"}
    # how long does the Depth-1 walk cost instead?
    started = time.perf_counter()
    walked: list = []
    queue = [""]
    requests = 0
    while queue:
        here = queue.pop(0)
        requests += 1
        try:
            kids = dav.propfind(here, depth="1")
        except NotFoundError:
            continue
        for prop in kids:
            if prop.path == here:
                continue
            if prop.is_dir:
                queue.append(prop.path)
            else:
                walked.append(prop.path)
    out["depth_1_walk"] = {"seconds": round(time.perf_counter() - started, 3),
                           "returned": len(walked),
                           "requests": requests}

    # 5: rate limiting under a burst ---------------------------------------------
    raw = WebDAV(dav.base_url, dav.username, None, timeout=dav.timeout,
                 allow_insecure_http=True, retries=0)
    raw._auth = dav._auth                     # reuse the credentials without re-reading them
    throttled = {"429": 0, "503": 0, "other": 0, "retry_after": []}
    for i in range(BURST):
        try:
            raw.propfind("many", depth="0")
        except RateLimitedError as exc:
            throttled["429"] += 1
            if (getattr(exc, "payload", None) or {}).get("retry_after"):
                throttled["retry_after"].append(exc.payload["retry_after"])
        except ServerError as exc:
            if getattr(exc, "status", 0) == 503:
                throttled["503"] += 1
            else:
                throttled["other"] += 1
        except Exception:                     # noqa: BLE001
            throttled["other"] += 1
    out["rate_limiting"] = {"requests": BURST, **throttled,
                            "observed": bool(throttled["429"] or throttled["503"])}
    out["server_throttled_earlier"] = len(dav.throttled)

    # 6: concurrent conditional create --------------------------------------------
    target = "race.txt"
    results: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(RACE_THREADS)

    def racer(index: int) -> None:
        client = WebDAV(dav.base_url, dav.username, None, timeout=dav.timeout,
                        allow_insecure_http=True, retries=0)
        client._auth = dav._auth
        try:
            barrier.wait(timeout=30)
            resp = client.put(target, f"racer {index}\n".encode(), if_none_match=True)
            code = resp.status
        except PreconditionFailed:
            code = 412
        except Exception as exc:              # noqa: BLE001
            code = getattr(exc, "status", 0) or 0
        with lock:
            results.append(code)

    threads = [threading.Thread(target=racer, args=(i,), daemon=True) for i in range(RACE_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    created = sum(1 for code in results if code in (200, 201, 204))
    refused = sum(1 for code in results if code == 412)
    out["concurrent_create"] = {"threads": RACE_THREADS, "created": created,
                                "precondition_failed": refused, "codes": results,
                                "atomic": created == 1}

    # verdict ---------------------------------------------------------------------
    strong = (out["if_none_match_star_on_existing_refused"] and out["stale_if_match_refused"]
              and out["concurrent_create"]["atomic"])
    out["guarantees"] = "strong" if strong else "best-effort"
    out["verdict"] = {
        "atomic_lock_create": out["concurrent_create"]["atomic"],
        "stale_write_guard": out["stale_if_match_refused"],
        "freshness_by_etag": bool(out["etag_on_put"]),
        "depth_infinity": out["depth_infinity"]["supported"],
    }
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Nextcloud WebDAV spike (PLAN §13)")
    parser.add_argument("--local", action="store_true",
                        help="run against the stdlib WebDAV fake instead of a real server")
    parser.add_argument("--wsgidav", action="store_true",
                        help="with --local: use WsgiDAV from temp/nextcloud/venv instead of the fake")
    parser.add_argument("--keep", action="store_true", help="do not delete the scratch folder")
    parser.add_argument("--files", type=int, default=FILE_COUNT, help="files for the PROPFIND test")
    parser.add_argument("--out", default=RESULT_PATH, help="where to write the JSON verdict")
    args = parser.parse_args(argv)

    server = None
    scratch_root = None
    if args.local:
        kind = "wsgidav" if args.wsgidav else "fake"
        server, scratch_root, url = _local_server(kind)
        user, password = "spike-user", "spike-password"
        folder = "spike-scratch"
        parent_root = url
        source = f"local-{kind}"
    else:
        url = _env("NC_URL")
        user = _env("NC_USER")
        password = _env("NC_APP_PASSWORD")
        folder = _env("NC_FOLDER")
        source = "nextcloud"
        if not (url and user and password and folder):
            print("NC_URL, NC_USER, NC_APP_PASSWORD and NC_FOLDER must all be set "
                  "(or use --local)", file=sys.stderr)
            return 2
        from online.nextcloud import webdav_root
        parent = os.path.dirname(folder.strip("/"))
        parent_root = (_env("NC_WEBDAV_ROOT")
                       or webdav_root(url, user, parent))

    allow_http = is_loopback(_host_only(url))
    parent_dav = WebDAV(parent_root, user, password, timeout=30.0,
                        allow_insecure_http=allow_http, retries=2, max_retry_wait=5.0)
    name = os.path.basename(folder.strip("/")) or "spike-scratch"
    folder_dav = WebDAV(f"{parent_root.rstrip('/')}/{urllib.parse.quote(name)}",
                        user, password, timeout=30.0,
                        allow_insecure_http=allow_http, retries=2, max_retry_wait=5.0)

    result: dict = {"source": source, "host": _host_only(url), "folder": name,
                    "checked_at": _now(), "ok": False}
    try:
        # the scratch folder is created here and nowhere else is written
        if parent_dav.exists(name):
            rows = parent_dav.propfind(name, depth="1")
            children = [p for p in rows if p.path and p.path != name]
            if children and not args.keep:
                raise SystemExit(f"{name!r} already exists and is not empty - refusing to touch it")
        else:
            parent_dav.mkcol(name)
        result.update(run(folder_dav, name, files=args.files))
        result["ok"] = True
    except SystemExit:
        raise
    except Exception as exc:                  # noqa: BLE001 - the verdict is the report
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["ok"] = False
    finally:
        if not args.keep:
            try:
                parent_dav.delete(name)       # recursive: only the scratch folder
            except Exception as exc:          # noqa: BLE001
                result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if server is not None:
            server.stop()
            if scratch_root and not args.keep:
                import shutil
                shutil.rmtree(scratch_root, ignore_errors=True)

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")

    _print_report(result)
    print(f"\nverdict written to {out_path}")
    return 0 if result.get("ok") else 1


def _print_report(result: dict) -> None:
    def yes(value) -> str:
        return "yes" if value else "NO"

    print(f"Nextcloud WebDAV spike - {result.get('source')} ({result.get('host')})")
    print(f"folder: {result.get('folder')}   checked: {result.get('checked_at')}")
    if not result.get("ok"):
        print(f"FAILED: {result.get('error')}")
        return
    print("")
    print(f"1. PUT If-None-Match: * on an existing file refused (412) ... {yes(result['if_none_match_star_on_existing_refused'])}")
    print(f"2. PUT If-Match with a stale etag refused (412) .............. {yes(result['stale_if_match_refused'])}")
    print(f"   PUT If-Match with the current etag accepted ............... {yes(result['fresh_if_match_accepted'])}")
    print(f"   a strong ETag is returned on write ........................ {yes(result['etag_on_put'])}")
    folder_etag = result.get("folder_etag") or {}
    print(f"3. folder ETag changes when a child changes .................. {yes(folder_etag.get('propagates'))}")
    print(f"   top folder ETag changes when a subtree changes ............ {yes((result.get('root_folder_etag') or {}).get('propagates'))}")
    infinity = result.get("depth_infinity") or {}
    if infinity.get("supported"):
        print(f"4. PROPFIND Depth: infinity .................................. yes "
              f"({infinity.get('returned')} entries, {infinity.get('seconds')} s, "
              f"complete={infinity.get('complete')})")
    else:
        print(f"4. PROPFIND Depth: infinity .................................. NO ({infinity.get('error')})")
    walk = result.get("depth_1_walk") or {}
    print(f"   Depth: 1 walk instead .................................... {walk.get('returned')} entries, "
          f"{walk.get('seconds')} s, {walk.get('requests')} requests")
    rate = result.get("rate_limiting") or {}
    print(f"5. rate limiting over {rate.get('requests')} quick requests ................. "
          f"429={rate.get('429')} 503={rate.get('503')} retry_after={rate.get('retry_after') or '-'}")
    race = result.get("concurrent_create") or {}
    print(f"6. {race.get('threads')} concurrent conditional creates .................... "
          f"created={race.get('created')} refused(412)={race.get('precondition_failed')} "
          f"atomic={yes(race.get('atomic'))}")
    print("")
    print(f"guarantees this share supports: {result.get('guarantees')}")


if __name__ == "__main__":
    raise SystemExit(main())
