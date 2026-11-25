"""Nextcloud mode at real scale: a local benchmark (2026-11-25).

The owner's project on a hosted Nextcloud holds ~25.5k files (~16 GB); its
first open "seemed stuck" (the core tier was downloaded one request at a time
*inside* the listing, 46 minutes), a reopen "started from scratch" (nothing was
saved until that listing ended) and papers were "missing" meanwhile (nothing
was listed until it ended).  This module rebuilds that shape on the stdlib
WebDAV fake with a realistic latency and measures:

* **first open** - ``connect`` + ``open`` until the listing is applied (the
  paper list can be served: "usable"), an on-demand read of a paper and of a
  core file that is not downloaded yet, and the core tier's background download;
* **interrupted first sync** - the first open runs in a child process that is
  *killed* part-way; the reopen must not download a file that is on disk;
* **reopen** of an intact mirror and a **no-change sync** (requests, seconds).

The share is synthetic (no project data): per-class counts and sizes follow the
real project (``nc-...`` mirror analysis, 2026-11-25): 6.9k core files
(~470 MB at ``--size-scale 1``), 18.6k on-demand files (written as small
placeholders: their bytes are never read by these scenarios).

Run (writes a JSON report)::

    python Scripts/coding_web/online/tests/nc_benchmark.py --out temp/ncfix/bench.json

Every mirror and the share live under ``--work`` (default: a fresh temp dir) -
never under ``%LOCALAPPDATA%``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_CODING_WEB = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SCRIPTS = os.path.abspath(os.path.join(_CODING_WEB, ".."))
for _p in (_SCRIPTS, _CODING_WEB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from online_shared import protocol  # noqa: E402

USER = "bench"
PASSWORD = "pw"


# ---------------------------------------------------------------------------
# the synthetic share
# ---------------------------------------------------------------------------

def _sized(rng: random.Random, mean: float, scale: float) -> int:
    """A lognormal size with the given mean (bytes), scaled."""
    sigma = 0.8
    mu = __import__("math").log(max(1.0, mean)) - sigma * sigma / 2
    return max(16, int(rng.lognormvariate(mu, sigma) * scale))


def build_share(root: str, *, keys: int = 862, size_scale: float = 1.0, seed: int = 7,
                placeholder: int = 512, extra_scale: float = 1.0) -> dict:
    """Write the synthetic project into *root*; returns ``{core, ondemand, core_bytes, paths}``."""
    rng = random.Random(seed)
    os.makedirs(root, exist_ok=True)
    files: dict[str, int] = {}          # rel -> size to write

    def add(rel: str, size: int) -> None:
        files[rel] = size

    names = [f"Study{i:04d} et al. ({2000 + i % 26})" for i in range(keys)]
    for i, k in enumerate(names):
        add(f"Text/{k}.txt", _sized(rng, 92_000, size_scale))
        add(f"Papers/{k}.pdf", 1_000_000)                       # on demand
        if i < 824:
            add(f"Extraction/{k}.json", _sized(rng, 66_000, size_scale))
            add(f"Extraction/{k}.md", _sized(rng, 40_000, size_scale))
        if i < 666:
            for tag in ("DS", "MM", "SB"):
                add(f"Codings/{k} - {tag}.json", 80_000)       # AI routes: on demand
                add(f"Codings/{k} - {tag}.md", 20_000)
        if i < 13:
            add(f"Codings/{k} - MF.json", _sized(rng, 28_000, size_scale))
        if i < 482:
            add(f"Reproduction/{k}.md", _sized(rng, 9_000, size_scale))
            add(f"Reproduction/{k} - SB.md", _sized(rng, 9_000, size_scale))
        if i < 540:
            d = f"Datasets/{k}"
            for tail in protocol.DATASET_CORE_CSVS[1:7]:          # 6 core sidecars
                if rng.random() < 0.95:
                    add(f"{d}/{k} - {tail}", _sized(rng, 17_000, size_scale))
            for tail in ("harmonized.csv", "prepared.csv", "conditions - SB.csv",
                         "exclusions - SB.csv", "harmonized - SB.csv", "variables - SB.csv"):
                add(f"{d}/{k} - {tail}", 400_000)               # shared, on demand
            for j in range(11):
                add(f"{d}/raw_{j:02d}.csv", 300_000)            # immutable raw data
        if i < 600:
            n_sup = 1 + (i % 3 == 0)
            for j in range(n_sup):
                add(f"Supplements/{k} Supplement {j + 1}.pdf", 2_000_000)
    for j in range(int(3017 * extra_scale)):
        add(f"Results/product_{j:04d}.csv", 50_000)
    for j in range(int(289 * extra_scale)):
        add(f"Scripts/script_{j:03d}.py", 8_000)
    for j in range(int(63 * extra_scale)):
        add(f"Server/file_{j:02d}.txt", 2_000)
    add("Project/project.json", 600)
    add("Project/criteria.json", 12_000)
    add("Project/scheme.json", 120_000)
    add("Project/master_base.xlsx", int(28_000_000 * size_scale))
    add("Project/master_records.csv.gz", int(23_000_000 * size_scale))
    add("Screening/MF.json", 22_000)
    add("Screening/OG.json", int(30_000_000 * size_scale))
    add("Fulltext/MF.json", 600_000)
    add("master_dataset.xlsx", int(33_800_000 * size_scale))
    for name in ("README.md", "AGENTS.md", "requirements.txt", "run_analysis.py"):
        add(name, 5_000)

    core = ondemand = 0
    core_bytes = 0
    for rel, size in files.items():
        full = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        is_core = protocol.is_core(rel)
        n = size if is_core else min(size, placeholder)
        with open(full, "wb") as fh:
            fh.write(rng.randbytes(min(n, 4096)) * (n // 4096) + rng.randbytes(n % 4096))
        if is_core:
            core += 1
            core_bytes += n
        else:
            ondemand += 1
    studio = os.path.join(root, ".studio")
    os.makedirs(studio, exist_ok=True)
    with open(os.path.join(studio, "members.json"), "w", encoding="utf-8") as fh:
        json.dump({"members": [{"username": USER, "role": "owner", "tags": ["MF"],
                                "tasks": list(protocol.TASKS)}]}, fh)
    return {"files": len(files), "core": core, "ondemand": ondemand, "core_bytes": core_bytes,
            "keys": names}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _store(base: str):
    from online.store import OnlineStore
    return OnlineStore(base_dir=base, use_keyring=False, autostart=True)


def _connect(store, url: str):
    projects = store.connect_nextcloud(url, USER, PASSWORD, "", webdav_root=url)
    if os.environ.get("NC_BENCH_NO_BULK") == "1":
        store.client.bulk_enabled = False       # the pre-archive path, for comparison
    return projects[0]["id"]


def _downloads(fake) -> tuple[int, int]:
    """(GETs of project files, GETs of .studio files) since the last reset."""
    with fake.state.lock:
        gets = dict(fake.state.gets)
    proj = sum(n for p, n in gets.items() if not p.startswith(".studio"))
    studio = sum(n for p, n in gets.items() if p.startswith(".studio"))
    return proj, studio


def _requests(fake) -> dict:
    with fake.state.lock:
        return dict(fake.state.methods)


def _core_heads(store) -> int:
    m = store.mirror
    with m.lock:
        return sum(1 for p in m.heads if protocol.is_core(p))


def _wait(pred, timeout: float, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def first_open(url: str, fake, base: str, info: dict, *, core_timeout: float) -> tuple[dict, object]:
    fake.resets()
    t0 = time.perf_counter()
    store = _store(base)
    pid = _connect(store, url)
    store.open(pid)
    t_open = time.perf_counter() - t0
    total = info["files"]
    _wait(lambda: len(store.mirror.remote) >= total, core_timeout)
    t_usable = time.perf_counter() - t0
    keys = info["keys"]
    out = {"open_returned_s": round(t_open, 2), "usable_s": round(t_usable, 2),
           "listed": len(store.mirror.remote)}
    # an on-demand paper and a core file at the end of the download queue
    t1 = time.perf_counter()
    try:
        store.ensure_local(f"Papers/{keys[-1]}.pdf")
        out["ensure_paper_s"] = round(time.perf_counter() - t1, 3)
    except Exception as exc:                 # noqa: BLE001
        out["ensure_paper_error"] = repr(exc)
    kd = keys[min(539, len(keys) - 1)]
    rel = f"Datasets/{kd}/{kd} - variables.csv"
    if not os.path.exists(store.mirror.abspath(rel)):
        rel = f"Text/{keys[-2]}.txt"
    out["core_file_local_before_ensure"] = os.path.exists(store.mirror.abspath(rel))
    t1 = time.perf_counter()
    try:
        store.ensure_local(rel)
        out["ensure_core_s"] = round(time.perf_counter() - t1, 3)
    except Exception as exc:                 # noqa: BLE001
        out["ensure_core_error"] = repr(exc)
    samples = []

    def _sample():
        st = store.status()
        dl = st.get("download") or {}
        samples.append({"t": round(time.perf_counter() - t0, 1), "heads": _core_heads(store),
                        "done": dl.get("files_done"), "total": dl.get("files_total"),
                        "eta_s": dl.get("eta_s"), "mb": round((dl.get("bytes_done") or 0) / 1e6, 1)})
        return _core_heads(store) >= info["core"]

    end = time.monotonic() + core_timeout
    last = 0.0
    while time.monotonic() < end:
        if time.monotonic() - last >= 2.0:
            last = time.monotonic()
            if _sample():
                break
        elif _core_heads(store) >= info["core"]:
            _sample()
            break
        time.sleep(0.1)
    out["core_done_s"] = round(time.perf_counter() - t0, 2)
    out["core_heads"] = _core_heads(store)
    out["core_complete"] = out["core_heads"] >= info["core"]
    proj, studio = _downloads(fake)
    out["project_gets"] = proj
    out["studio_gets"] = studio
    out["archive_requests"] = len(fake.state.archive_requests)
    out["requests"] = _requests(fake)
    out["progress_samples"] = samples[:: max(1, len(samples) // 12)] + samples[-1:]
    return out, store


def reopen(url: str, fake, base: str, info: dict) -> tuple[dict, object]:
    fake.resets()
    t0 = time.perf_counter()
    store = _store(base)
    pid = _connect(store, url)
    store.open(pid, start=False)
    t_open = time.perf_counter() - t0
    store.sync_now()
    t_sync = time.perf_counter() - t0
    proj, studio = _downloads(fake)
    return {"open_returned_s": round(t_open, 2), "first_round_s": round(t_sync, 2),
            "project_gets": proj, "studio_gets": studio, "requests": _requests(fake),
            "mode": store.status().get("mode"), "core_heads": _core_heads(store)}, store


def no_change(store, fake, rounds: int = 3) -> dict:
    try:
        store.presence(None, None)              # what the UI does every 15 s
        store.presence(None, None)
    except Exception:                         # noqa: BLE001 - an old build without presence
        pass
    times = []
    reqs = []
    for _ in range(rounds):
        fake.resets()
        t = time.perf_counter()
        store.sync_now()
        times.append(round(time.perf_counter() - t, 3))
        reqs.append(_requests(fake))
    proj, _studio = _downloads(fake)
    return {"seconds": times, "requests": reqs, "project_gets_last_round": proj}


def _child(url: str, base: str) -> None:
    """The first open in a child process (killed by the parent)."""
    store = _store(base)
    pid = _connect(store, url)
    store.open(pid)
    print("OPENED", flush=True)
    while True:
        time.sleep(1)


def interrupted(url: str, fake, base: str, info: dict, *, kill_at: float,
                kill_after: float = 0.0) -> dict:
    """Kill the first sync part-way, then reopen: what is downloaded again?"""
    fake.resets()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([_SCRIPTS, _CODING_WEB])
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--child", url, base],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    mirror_root = None
    t0 = time.perf_counter()
    want = int(info["core"] * kill_at)
    try:
        while time.perf_counter() - t0 < (kill_after or 1800):
            if mirror_root is None:
                for slug in (os.listdir(base) if os.path.isdir(base) else []):
                    for pid in os.listdir(os.path.join(base, slug)):
                        mirror_root = os.path.join(base, slug, pid)
            if mirror_root is not None:
                n = 0
                for top in ("Text", "Extraction", "Datasets", "Reproduction", "Codings"):
                    for _r, _d, fs in os.walk(os.path.join(mirror_root, top)):
                        n += len(fs)
                if n >= want:
                    break
            time.sleep(0.5)
    finally:
        proc.kill()                                   # a hard kill: no close(), no save
        proc.wait(30)
    t_kill = time.perf_counter() - t0
    on_disk = set()
    for top in ("Project", "Screening", "Fulltext", "Text", "Extraction", "Datasets", "Reproduction",
                "Codings"):
        for r, _d, fs in os.walk(os.path.join(mirror_root, top)):
            for f in fs:
                on_disk.add(os.path.relpath(os.path.join(r, f), mirror_root).replace("\\", "/"))
    if os.path.exists(os.path.join(mirror_root, "master_dataset.xlsx")):
        on_disk.add("master_dataset.xlsx")
    fake.resets()
    t1 = time.perf_counter()
    store = _store(base)
    pid = _connect(store, url)
    store.open(pid, start=False)
    t_open = time.perf_counter() - t1
    for _ in range(200):
        store.sync_now()
        if _core_heads(store) >= info["core"]:
            break
    t_done = time.perf_counter() - t1
    with fake.state.lock:
        gets = dict(fake.state.gets)
    again = sorted(p for p in gets if p in on_disk)
    out = {"killed_after_s": round(t_kill, 1), "files_on_disk_at_kill": len(on_disk),
           "reopen_open_s": round(t_open, 2), "resume_complete_s": round(t_done, 2),
           "redownloaded": len(again), "redownloaded_sample": again[:5],
           "project_gets_after_reopen": sum(n for p, n in gets.items() if not p.startswith(".studio")),
           "core_heads": _core_heads(store), "core": info["core"]}
    store.close()
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--child", nargs=2, metavar=("URL", "BASE"), help=argparse.SUPPRESS)
    ap.add_argument("--out", default="")
    ap.add_argument("--work", default="")
    ap.add_argument("--keys", type=int, default=862)
    ap.add_argument("--size-scale", type=float, default=1.0)
    ap.add_argument("--latency", default="0.03,0.08", help="seconds per request: lo,hi")
    ap.add_argument("--bandwidth", type=float, default=20e6, help="bytes/s per response (0 = unlimited)")
    ap.add_argument("--core-timeout", type=float, default=1800.0)
    ap.add_argument("--kill-at", type=float, default=0.4)
    ap.add_argument("--kill-after", type=float, default=0.0,
                    help="kill the child after this many seconds at the latest (0 = no limit)")
    ap.add_argument("--skip-interrupt", action="store_true")
    ap.add_argument("--no-bulk", action="store_true",
                    help="disable folder archives (the pre-0.5 per-file path, for comparison)")
    args = ap.parse_args(argv)
    if args.no_bulk:
        os.environ["NC_BENCH_NO_BULK"] = "1"
    if args.child:
        _child(*args.child)
        return 0
    from online.tests.webdav_fake import WebDAVFake

    work = args.work or tempfile.mkdtemp(prefix="ncbench-")
    os.makedirs(work, exist_ok=True)
    # never the user's real mirrors
    os.environ["CODINGSTUDIO_ONLINE_DIR"] = os.path.join(work, "default-online")
    lo, hi = (float(x) for x in args.latency.split(","))
    share = os.path.join(work, "share")
    if os.path.isdir(share):
        shutil.rmtree(share)
    t = time.perf_counter()
    info = build_share(share, keys=args.keys, size_scale=args.size_scale)
    built = time.perf_counter() - t
    fake = WebDAVFake(share, latency=(lo, hi), fast_etags=True, bandwidth=args.bandwidth or None)
    url = fake.start()
    report = {"share": {k: v for k, v in info.items() if k != "keys"}, "build_s": round(built, 1),
              "latency": [lo, hi], "bandwidth": args.bandwidth}
    try:
        t = time.perf_counter()
        fake.resets()
        from online.webdav import WebDAV
        rows = WebDAV(url, USER, PASSWORD, allow_insecure_http=True).propfind("", depth="infinity")
        report["propfind_infinity"] = {"seconds": round(time.perf_counter() - t, 2), "entries": len(rows)}
        print("propfind", report["propfind_infinity"], flush=True)

        base1 = os.path.join(work, "client-1")
        res, store = first_open(url, fake, base1, info, core_timeout=args.core_timeout)
        report["first_open"] = res
        print("first_open", {k: v for k, v in res.items() if k != "progress_samples"}, flush=True)
        store.close()

        res, store = reopen(url, fake, base1, info)
        report["reopen"] = res
        print("reopen", res, flush=True)
        report["no_change_sync"] = no_change(store, fake)
        print("no_change", report["no_change_sync"], flush=True)
        store.close()

        if not args.skip_interrupt:
            base2 = os.path.join(work, "client-2")
            report["interrupted"] = interrupted(url, fake, base2, info, kill_at=args.kill_at,
                                                kill_after=args.kill_after)
            print("interrupted", report["interrupted"], flush=True)
    finally:
        fake.stop()
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
    print(json.dumps({k: v for k, v in report.items() if k not in ("first_open",)}, indent=1)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
