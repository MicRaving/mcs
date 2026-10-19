#!/usr/bin/env python
"""Dataset acquisition helper for the misinformation-intervention meta-analysis.

Given a study key and a URL/DOI, resolve the platform, download every public
file (recursively where possible) into Datasets/<Key>/, and write
Datasets/<Key>/SOURCE.md provenance.  Raw originals keep their names.

Usage:
  python acquire_datasets.py <Key> <url-or-doi> [--dry] [--timeout SECONDS]

Also importable: `run(key, url, dry=False, timeout=None)` performs the same
acquisition and, for every run (success or failure), appends one JSON object to
Results/acquisition_log.jsonl and writes Datasets/<Key>/SOURCE.md.

Exit code 0 when at least one file is downloaded or already present.
Prints a JSON summary on stdout.
"""
import json
import os
import re
import shutil
import sys
import time
import zipfile
import io
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "Datasets")
TEMP = os.path.join(ROOT, "temp")
RESULTS = os.path.join(ROOT, "Results")
os.makedirs(TEMP, exist_ok=True)

# Machine-readable attempt ledger (one JSON object per line, appended per run).
LOG_PATH = os.path.join(RESULTS, "acquisition_log.jsonl")

UA = {"User-Agent": "Mozilla/5.0 (research-meta dataset acquisition)"}
TIMEOUT = 60
# When set (by run()/--timeout), every request timeout is capped to this many
# seconds so a single study cannot block the sweep indefinitely.
TIMEOUT_CAP = None
BIG = 200 * 1024 * 1024  # do not silently download >200MB single files


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def safe(name):
    name = str(name).replace("\\", "_").replace("/", "_")
    name = re.sub(r'[<>:"|?*]', "_", name)
    return name.strip() or "file"


def req(url, tries=4, timeout=None, stream=False, allow_redirects=True, **kw):
    if timeout is None:
        timeout = TIMEOUT
    if TIMEOUT_CAP:
        timeout = min(timeout, TIMEOUT_CAP)
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, stream=stream,
                             allow_redirects=allow_redirects, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last = r
                time.sleep(1.5 * (i + 1))
                continue
            return r
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    if isinstance(last, Exception):
        log("REQ FAIL", url, repr(last))
        return None
    return last


def save(dest, name, content, results, link=""):
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, safe(name))
    if os.path.exists(path):
        if os.path.getsize(path) == len(content):
            results.append({"filename": os.path.basename(path), "status": "exists",
                            "bytes": len(content), "link": link})
            return
        root, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(f"{root}_{i}{ext}"):
            i += 1
        path = f"{root}_{i}{ext}"
    with open(path, "wb") as f:
        f.write(content)
    results.append({"filename": os.path.basename(path), "status": "downloaded",
                    "bytes": len(content), "link": link})


# ---------------------------------------------------------------- OSF
def osf_walk(url, dest, vo, results, depth=0):
    if depth > 6:
        return
    params = {"page[size]": 100}
    if vo:
        params["view_only"] = vo
    r = req(url, params=params, timeout=100, tries=3)
    if r is None or r.status_code != 200:
        log("OSF list fail", url, getattr(r, "status_code", "?"))
        results.append({"filename": "", "status": "err:list",
                        "bytes": 0, "link": url})
        return
    data = r.json()
    for item in data.get("data", []):
        attrs = item.get("attributes", {})
        kind = attrs.get("kind")
        name = attrs.get("name", "file")
        if kind == "folder":
            link = (item.get("relationships", {}).get("files", {})
                    .get("links", {}).get("related", {}).get("href"))
            if link:
                sep = "&" if "?" in link else "?"
                sub = link + (f"{sep}view_only={vo}" if vo and "view_only" not in link else "")
                osf_walk(sub, os.path.join(dest, safe(name)), None, results, depth + 1)
        else:
            dl = item.get("links", {}).get("download")
            if not dl:
                continue
            if vo and "view_only" not in dl:
                dl += ("&" if "?" in dl else "?") + f"view_only={vo}"
            size = attrs.get("size") or 0
            if size and size > BIG:
                results.append({"filename": safe(name), "status": "skip:too_big",
                                "bytes": size, "link": dl})
                continue
            rr = req(dl, timeout=180)
            if rr is not None and rr.status_code == 200:
                save(dest, name, rr.content, results, dl)
            else:
                results.append({"filename": safe(name),
                                "status": f"err:{getattr(rr,'status_code','?')}",
                                "bytes": 0, "link": dl})
    nxt = data.get("links", {}).get("next")
    if nxt:
        osf_walk(nxt, dest, vo, results, depth)


def _osf_get(url, vo, timeout=100):
    if vo and "view_only" not in url:
        url += ("&" if "?" in url else "?") + f"view_only={vo}"
    return req(url, timeout=timeout)


def fetch_osf(node, dest, vo=None):
    """Crawl an OSF node/registration: all file providers + child components."""
    res = []
    seen = set()

    def resolve():
        for kind in ("nodes", "registrations"):
            r = _osf_get(f"https://api.osf.io/v2/{kind}/{node}/", vo)
            if r is not None and r.status_code == 200:
                return kind, node
        r = _osf_get(f"https://api.osf.io/v2/guids/{node}/", vo)
        if r is not None and r.status_code == 200:
            d = r.json()["data"]
            t = d.get("type", "")
            if "preprint" in t:
                rp = _osf_get(f"https://api.osf.io/v2/preprints/{d.get('id')}/", vo)
                if rp is not None and rp.status_code == 200:
                    href = (rp.json()["data"].get("relationships", {}).get("node", {})
                            .get("links", {}).get("related", {}).get("href"))
                    if href:
                        return "nodes", href.rstrip("/").split("/")[-1]
            if "registration" in t:
                return "registrations", d.get("id", node)
            return "nodes", d.get("id", node)
        return None, node

    def walk_object(kind, oid, depth=0):
        if depth > 3 or oid in seen:
            return
        seen.add(oid)
        rp = _osf_get(f"https://api.osf.io/v2/{kind}/{oid}/files/", vo)
        if rp is not None and rp.status_code == 200:
            for prov in rp.json().get("data", []):
                link = (prov.get("relationships", {}).get("files", {}).get("links", {})
                        .get("related", {}).get("href"))
                if link:
                    osf_walk(link, dest, vo, res)
        elif rp is not None:
            res.append({"filename": "", "status": f"err:providers{rp.status_code}", "bytes": 0,
                        "link": f"https://api.osf.io/v2/{kind}/{oid}/files/"})
        # children
        rc = _osf_get(f"https://api.osf.io/v2/{kind}/{oid}/children/", vo)
        if rc is not None and rc.status_code == 200:
            for ch in rc.json().get("data", []):
                walk_object(kind, ch.get("id"), depth + 1)
            nxt = rc.json().get("links", {}).get("next")
            while nxt:
                rc2 = _osf_get(nxt, vo)
                if rc2 is None or rc2.status_code != 200:
                    break
                for ch in rc2.json().get("data", []):
                    walk_object(kind, ch.get("id"), depth + 1)
                nxt = rc2.json().get("links", {}).get("next")

    kind, oid = resolve()
    if kind is None:
        return [{"filename": "", "status": "err:resolve", "bytes": 0, "link": f"https://osf.io/{node}/"}]
    walk_object(kind, oid)
    return res


# ---------------------------------------------------------------- Zenodo
def fetch_zenodo(record, dest):
    res = []
    r = req(f"https://zenodo.org/api/records/{record}")
    if r is None or r.status_code != 200:
        return [{"filename": "", "status": f"err:{getattr(r,'status_code','?')}", "bytes": 0, "link": ""}]
    for f in r.json().get("files", []):
        url = f.get("links", {}).get("self") or f.get("links", {}).get("download")
        rr = req(url, timeout=300)
        if rr is not None and rr.status_code == 200:
            save(dest, f.get("key", "file"), rr.content, res, url)
        else:
            res.append({"filename": safe(f.get("key", "file")),
                        "status": f"err:{getattr(rr,'status_code','?')}", "bytes": 0, "link": url})
    return res


# ---------------------------------------------------------------- GitHub
def fetch_github(repo, dest):
    res = []
    meta = req(f"https://api.github.com/repos/{repo}")
    branches = ["main", "master"]
    if meta is not None and meta.status_code == 200:
        branches = [meta.json().get("default_branch", "main")] + branches
    for br in branches:
        r = req(f"https://github.com/{repo}/archive/refs/heads/{br}.zip", timeout=300)
        if r is not None and r.status_code == 200 and r.content[:2] == b"PK":
            try:
                zf = zipfile.ZipFile(io.BytesIO(r.content))
            except Exception:
                continue
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                parts = n.split("/", 1)
                rel = parts[1] if len(parts) > 1 else n
                save(dest, rel, zf.read(n), res, f"https://github.com/{repo}")
            return res
    return [{"filename": "", "status": "err:zip", "bytes": 0, "link": f"https://github.com/{repo}"}]


# ---------------------------------------------------------------- Figshare
def fetch_figshare(arg, dest):
    res = []
    aid = arg
    m = re.search(r"figshare\.(\d+)", arg)
    if m:
        aid = m.group(1)
    m = re.search(r"/articles?/[^/]+/(\d+)", arg)
    if m:
        aid = m.group(1)
    r = req(f"https://api.figshare.com/v2/articles/{aid}")
    if r is None or r.status_code != 200:
        return [{"filename": "", "status": f"err:{getattr(r,'status_code','?')}", "bytes": 0, "link": arg}]
    for f in r.json().get("files", []):
        url = f["download_url"]
        rr = req(url, timeout=300)
        if rr is not None and rr.status_code == 200:
            save(dest, f["name"], rr.content, res, url)
        else:
            res.append({"filename": safe(f["name"]),
                        "status": f"err:{getattr(rr,'status_code','?')}", "bytes": 0, "link": url})
    return res


# ---------------------------------------------------------------- Dataverse
def fetch_dataverse(arg, dest):
    res = []
    dsid = arg
    m = re.search(r"10\.7910/DVN/[A-Z0-9]+", arg, re.I)
    if m:
        dsid = m.group(0)
    elif re.search(r"/dataset\.xhtml", arg):
        dsid = re.sub(r"^https?://.*?persistentId=", "", arg).replace("doi:", "")
    dsid = dsid.replace("doi:", "").strip().strip("/")
    pid = "doi:" + dsid
    for base in ("https://dataverse.harvard.edu", "https://dataverse.nl",
                 "https://dataverse.no", "https://dataverse.unc.edu"):
        r = req(f"{base}/api/datasets/:persistentId/", params={"persistentId": pid})
        if r is not None and r.status_code == 200:
            try:
                payload = r.json()
            except Exception:
                continue
            for f in payload.get("data", {}).get("latestVersion", {}).get("files", []):
                df = f.get("dataFile", {})
                fid = df.get("id")
                name = df.get("filename", "file")
                rr = req(f"{base}/api/access/datafile/{fid}", timeout=300)
                if rr is not None and rr.status_code == 200:
                    save(dest, name, rr.content, res, f"{base}/api/access/datafile/{fid}")
                else:
                    res.append({"filename": safe(name),
                                "status": f"err:{getattr(rr,'status_code','?')}", "bytes": 0, "link": ""})
            return res
    return [{"filename": "", "status": "err:notfound", "bytes": 0, "link": arg}]


# ---------------------------------------------------------------- Dryad
def fetch_dryad(doi, dest):
    res = []
    d = doi.replace("doi:", "").strip()
    if not d.startswith("10.5061"):
        d = "10.5061/dryad." + d
    r = req(f"https://datadryad.org/api/v2/datasets/{d}")
    if r is None or r.status_code != 200:
        r = req(f"https://datadryad.org/api/v2/search", params={"q": d})
        return [{"filename": "", "status": f"err:{getattr(r,'status_code','?')}", "bytes": 0, "link": doi}]
    for f in r.json().get("_embedded", {}).get("stash:files", []):
        url = f.get("_links", {}).get("stash:download", {}).get("href")
        name = f.get("path", "file")
        if not url:
            continue
        rr = req(url, timeout=300)
        if rr is not None and rr.status_code == 200:
            save(dest, name, rr.content, res, url)
        else:
            res.append({"filename": safe(name), "status": f"err:{getattr(rr,'status_code','?')}", "bytes": 0, "link": url})
    return res


# ---------------------------------------------------------------- PsychArchives
def fetch_psycharchives(doi, dest):
    """Resolve the DOI to a PsychArchives item page and download its bitstreams."""
    res = []
    r = req(doi, timeout=60)
    if r is None or r.status_code != 200:
        return [{"filename": "", "status": f"err:{getattr(r,'status_code','?')}", "bytes": 0, "link": doi}]
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "/bitstream/" in h or "/api/core/bitstreams/" in h:
            if h.startswith("/"):
                from urllib.parse import urljoin
                h = urljoin(r.url, h)
            links.append(h)
    for h in dict.fromkeys(links):
        rr = req(h, timeout=300)
        if rr is not None and rr.status_code == 200 and rr.content:
            cd = rr.headers.get("Content-Disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)', cd)
            nm = m.group(1) if m else h.rstrip("/").split("/")[-1]
            save(dest, nm, rr.content, res, h)
        else:
            res.append({"filename": h.split("/")[-1],
                        "status": f"err:{getattr(rr,'status_code','?')}", "bytes": 0, "link": h})
    # DSpace 7 REST fallback
    if not any(f["status"] in ("downloaded", "exists") for f in res):
        m = re.search(r"/item/([0-9a-f-]{36})", r.url)
        if m:
            api = req(f"{r.url.split('/en/item')[0]}/server/api/core/items/{m.group(1)}/bundles",
                      timeout=60)
            if api is not None and api.status_code == 200:
                for b in api.json().get("_embedded", {}).get("bundles", []):
                    bl = b.get("_links", {}).get("bitstreams", {}).get("href")
                    if not bl:
                        continue
                    bb = req(bl, timeout=60)
                    if bb is None or bb.status_code != 200:
                        continue
                    for bs in bb.json().get("_embedded", {}).get("bitstreams", []):
                        c = bs.get("_links", {}).get("content", {}).get("href")
                        if not c:
                            continue
                        cc = req(c, timeout=300)
                        if cc is not None and cc.status_code == 200:
                            save(dest, bs.get("name", "file"), cc.content, res, c)
    return res



# ---------------------------------------------------------------- generic
def fetch_generic(url, dest):
    """Follow redirects and scrape download links (data/code files)."""
    res = []
    r = req(url, timeout=60)
    if r is None:
        return [{"filename": "", "status": "err:req", "bytes": 0, "link": url}]
    final = r.url
    ct = (r.headers.get("Content-Type", "") or "").lower()
    bare = final.lower().split("?")[0]
    doc_exts = (".csv", ".xlsx", ".xls", ".xlsm", ".sav", ".dta", ".rds", ".rdata",
                ".zip", ".pdf", ".docx", ".doc", ".pptx", ".txt", ".json", ".do", ".r", ".rmd")
    is_doc = any(k in ct for k in ("zip", "octet-stream", "spreadsheet", "pdf",
                                   "wordprocessingml", "msword", "excel", "csv",
                                   "powerpoint", "text/plain", "json"))
    if r.status_code == 200 and (is_doc or bare.endswith(doc_exts)):
        return save(dest, bare.split("/")[-1] or "download", r.content, res, final) or res
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return [{"filename": "", "status": "err:parse", "bytes": 0, "link": final}]
    exts = (".csv", ".xlsx", ".xls", ".sav", ".dta", ".rds", ".rdata", ".zip",
            ".json", ".do", ".r", ".rmd", ".py", ".ipynb", ".txt", ".pdf", ".docx", ".por")
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(final, href)
        if not href.startswith("http"):
            continue
        if href in seen:
            continue
        seen.add(href)
        if any(href.lower().split("?")[0].endswith(e) for e in exts) or "download" in href.lower():
            rr = req(href, timeout=300)
            if rr is not None and rr.status_code == 200 and len(rr.content) > 0:
                nm = href.split("/")[-1].split("?")[0] or "download"
                save(dest, nm, rr.content, res, href)
    return res or [{"filename": "", "status": "err:nofiles", "bytes": 0, "link": final}]


# ---------------------------------------------------------------- classify
OSF_RE = re.compile(r"osf\.io/(?:view_only=|)([a-z0-9]{5,})", re.I)
OSF_DOI_RE = re.compile(r"10\.17605/OSF\.IO/([A-Z0-9]+)", re.I)


def classify(url):
    """Return (platform, payload, view_only)."""
    u = url.strip()
    vo = None
    m = re.search(r"[?&]view_only=([^&\s)]+)", u)
    if m:
        vo = m.group(1)
    m = OSF_DOI_RE.search(u)
    if m:
        return "osf", m.group(1).lower(), vo
    if "osf.io" in u:
        m = OSF_RE.search(u)
        if m:
            node = m.group(1)
            if node.lower() in ("view", "search", "myprojects", "dashboard"):
                return "generic", u, vo
            return "osf", node.lower(), vo
    z = re.search(r"10\.5281/zenodo\.(\d+)", u) or re.search(r"zenodo\.org/records?/(\d+)", u)
    if z:
        return "zenodo", z.group(1), None
    f = re.search(r"10\.6084/m9\.figshare\.(\d+)", u) or re.search(r"/articles?/[^/]+/(\d+)", u)
    if not f and "figshare" in u:
        f = re.search(r"/(\d{6,})(?:/\d+)?(?:[?#].*)?$", u.rstrip("/"))
    if f and ("figshare" in u or "10.6084" in u):
        return "figshare", f.group(1), None
    if "dataverse" in u or re.search(r"10\.7910/DVN", u, re.I):
        return "dataverse", u, None
    if "dryad" in u or u.startswith("10.5061") or "10.5061" in u:
        return "dryad", u, None
    if "psycharchives" in u or "10.23668" in u:
        return "psycharchives", u, None
    g = re.search(r"github\.com/([^/\s]+/[^/\s#?]+)", u)
    if g:
        repo = g.group(1).rstrip(".git")
        return "github", repo, None
    if "10.3886" in u:
        return "icpsr", u, None
    return "generic", u, vo


def acquire(key, url, dest_root=DATA, dry=False):
    platform, payload, vo = classify(url)
    dest = os.path.join(dest_root, key)
    existed = os.path.isdir(dest)
    done_marker = os.path.exists(os.path.join(dest, "SOURCE.md"))
    summary = {"key": key, "url": url, "platform": platform, "payload": payload,
               "view_only": bool(vo), "dest": dest, "existed": existed,
               "resumed": existed and not done_marker, "files": [], "error": ""}
    if dry:
        return summary
    if done_marker:
        log("SKIP completed folder", dest)
        summary["skipped"] = True
        summary["error"] = "completed_folder"
        return summary
    os.makedirs(dest, exist_ok=True)
    log(f"[{key}] {platform} {payload} -> {dest}")
    try:
        if platform == "osf":
            res = fetch_osf(payload, dest, vo)
        elif platform == "zenodo":
            res = fetch_zenodo(payload, dest)
        elif platform == "figshare":
            res = fetch_figshare(payload, dest)
        elif platform == "dataverse":
            res = fetch_dataverse(payload, dest)
        elif platform == "dryad":
            res = fetch_dryad(payload, dest)
        elif platform == "github":
            res = fetch_github(payload, dest)
        elif platform == "psycharchives":
            res = fetch_psycharchives(payload, dest)
        else:
            res = fetch_generic(payload, dest)
    except Exception as e:  # noqa
        log("EXC", key, repr(e))
        summary["error"] = f"{type(e).__name__}: {e}"
        res = [{"filename": "", "status": "err:exc" + type(e).__name__, "bytes": 0, "link": url}]
    summary["files"] = res
    ok = [f for f in res if f["status"] in ("downloaded", "exists")]
    summary["ok"] = len(ok)
    summary["bytes"] = sum(f["bytes"] for f in ok)
    if not summary["error"]:
        errs = [f for f in res if str(f.get("status", "")).startswith("err")]
        if errs and not ok:
            summary["error"] = "; ".join(sorted({str(f["status"]) for f in errs}))
        elif not res:
            summary["error"] = "no_files_returned"
    return summary


# ---------------------------------------------------------------- ledger + run
def _log_record(summary, dry, error=""):
    """Build the machine-readable ledger record for one acquisition run."""
    files = summary.get("files", []) or []
    n_downloaded = sum(1 for f in files if f.get("status") == "downloaded")
    n_retrieved = sum(1 for f in files if f.get("status") in ("downloaded", "exists"))
    n_not = sum(1 for f in files if f.get("status") not in ("downloaded", "exists"))
    return {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "key": summary.get("key"),
        "url_or_doi": summary.get("url"),
        "platform": summary.get("platform"),
        "dry": bool(dry),
        "ok": bool((not dry) and n_retrieved > 0 and not summary.get("skipped")),
        "n_downloaded": n_downloaded,
        "n_not_retrieved": n_not,
        "files": [{"filename": f.get("filename", ""), "status": f.get("status", ""),
                   "bytes": f.get("bytes", 0), "link": f.get("link", "")} for f in files],
        "dest": summary.get("dest"),
        "error": error if error else summary.get("error", ""),
    }


def append_log(record, path=LOG_PATH):
    """Append one JSON object to Results/acquisition_log.jsonl (created if needed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(key, url, dest_root=DATA, dry=False, timeout=None, keep_failed_dir=True,
        log_path=LOG_PATH):
    """Acquire one study and persist a machine-readable attempt record.

    Returns the summary dict (same shape as `acquire`, plus n_downloaded /
    n_not_retrieved / ok).  One line is appended to Results/acquisition_log.jsonl
    for every run, success or failure.

    keep_failed_dir=False (used by the sweep) removes the Datasets/<Key>/ folder
    that this call created when not a single file could be written, so failed
    attempts do not inflate `state.json: datasets.present`.  The attempt stays
    documented in Results/acquisition_log.jsonl.
    """
    global TIMEOUT, TIMEOUT_CAP
    old_timeout, old_cap = TIMEOUT, TIMEOUT_CAP
    if timeout:
        TIMEOUT = int(timeout)
        TIMEOUT_CAP = int(timeout)
    try:
        dest = os.path.join(dest_root, key)
        created = not os.path.isdir(dest)
        summary = acquire(key, url, dest_root=dest_root, dry=dry)
        retrieved = [f for f in summary.get("files", [])
                     if f.get("status") in ("downloaded", "exists")]
        if not dry:
            if keep_failed_dir or retrieved:
                write_source(key, summary, dest_root=dest_root)
            elif created and os.path.isdir(dest):
                # nothing was written into a folder we just created -> roll it back
                shutil.rmtree(dest, ignore_errors=True)
        files = summary.get("files", []) or []
        summary["n_downloaded"] = sum(1 for f in files if f.get("status") == "downloaded")
        summary["n_not_retrieved"] = sum(
            1 for f in files if f.get("status") not in ("downloaded", "exists"))
        # NB: summary["ok"] keeps acquire()'s original integer semantics (count of
        # downloaded+existing files); the ledger's boolean `ok` is derived separately.
        record = _log_record(summary, dry, error="" if dry else summary.get("error", ""))
        append_log(record, path=log_path)
        return summary
    finally:
        TIMEOUT, TIMEOUT_CAP = old_timeout, old_cap


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Acquire a study's public dataset into Datasets/<Key>/ and "
                    "append the attempt to Results/acquisition_log.jsonl.")
    ap.add_argument("key", nargs="?", help="study key, e.g. 'Biddlestone et al. (2025 a)'")
    ap.add_argument("url", nargs="?", help="public URL or DOI for the dataset")
    ap.add_argument("--dry", action="store_true", help="resolve only, download nothing")
    ap.add_argument("--timeout", type=int, default=None,
                    help="per-request timeout cap in seconds (default 60)")
    a = ap.parse_args()
    if not a.key or not a.url:
        ap.error("the following arguments are required: key, url")
    s = run(a.key, a.url, dry=a.dry, timeout=a.timeout)
    print(json.dumps(s, ensure_ascii=False, indent=1))


def write_source(key, summary, extra=None, dest_root=DATA):
    dest = os.path.join(dest_root, key)
    if not os.path.isdir(dest):
        return
    ok = [f for f in summary.get("files", []) if f["status"] in ("downloaded", "exists")]
    lines = [f"# {key} - data source provenance", ""]
    lines.append(f"- URL/DOI: {summary.get('url','')}")
    lines.append(f"- Platform: {summary.get('platform','')}")
    if summary.get("view_only"):
        lines.append("- Access: OSF view-only (private) link token")
    lines.append(f"- Access date: {time.strftime('%Y-%m-%d')}")
    if extra:
        for k, v in extra.items():
            lines.append(f"- {k}: {v}")
    lines.append(f"- Files downloaded ({len(ok)}):")
    for f in ok:
        lines.append(f"  - {f['filename']} ({f['bytes']} bytes)")
    bad = [f for f in summary.get("files", []) if f["status"] not in ("downloaded", "exists")]
    if bad:
        lines.append(f"- Not retrieved ({len(bad)}):")
        for f in bad[:40]:
            lines.append(f"  - {f['filename']} [{f['status']}] {f['link']}")
    lines.append("")
    with open(os.path.join(dest, "SOURCE.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    key = sys.argv[1]
    url = sys.argv[2]
    dry = "--dry" in sys.argv
    s = acquire(key, url, dry=dry)
    if not dry:
        write_source(key, s)
    print(json.dumps(s, ensure_ascii=False, indent=1))
