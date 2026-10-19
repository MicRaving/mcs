"""Re-acquisition queue for studies recorded as `datasets.missing`.

WHY: five studies this campaign (Ager, DeVerna, Lutzke et al., Sabherwal, Meiske) were
recorded as "no data / acquisition FAILED" and turned out to have deposits that **arrived after
that verdict**.  Nothing re-checked, so `state.json → datasets.missing` was a snapshot with an
unknown shelf life, and two of the five studies became reproducible the moment the data were
found.  This script makes staleness measurable and gives the re-check a queue.

    python Scripts/recheck_acquisitions.py --plan        # build the queue + stamp columns
    python Scripts/recheck_acquisitions.py --mark "<Key>" <date|found|missing> [url]
    python Scripts/recheck_acquisitions.py --status

Outputs
  Results/acquisition_attempts.csv   gains a `last_checked_utc` column (blank = never re-checked)
  temp/requisition/queue.csv         the studies to re-probe, most-stale first
  temp/requisition/state.json        per-study state for the dispatcher
"""
from __future__ import annotations
import os, csv, json, glob, sys, datetime
import pandas as pd

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATT = os.path.join(B, "Results", "acquisition_attempts.csv")
STATE = os.path.join(B, "Results", "state.json")
OUT = os.path.join(B, "temp", "requisition")
POOL = int(os.environ.get("RECHECK_POOL", "24"))
NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def missing_keys():
    st = json.load(open(STATE, encoding="utf-8"))
    d = st.get("datasets", {}).get("missing", [])
    if isinstance(d, dict):
        d = list(d)
    return sorted(str(k) for k in d)


def plan():
    os.makedirs(OUT, exist_ok=True)
    att = pd.read_csv(ATT) if os.path.isfile(ATT) else pd.DataFrame()
    if "last_checked_utc" not in att.columns:
        # First run: no study has ever been re-checked, so the column is blank by
        # construction -- that blankness is itself the finding (the snapshot is undated).
        att["last_checked_utc"] = ""
    keys = missing_keys()
    have = set()
    if "study" in att.columns:
        have = set(att["study"].astype(str))
    add = [{"study": k, "last_checked_utc": ""} for k in keys if k not in have]
    if add:
        att = pd.concat([att, pd.DataFrame(add)], ignore_index=True)
    att.to_csv(ATT, index=False, encoding="utf-8-sig")
    rows = []
    for k in keys:
        r = att[att.get("study", pd.Series(dtype=str)).astype(str) == k]
        last = str(r.iloc[0].get("last_checked_utc") or "") if len(r) else ""
        rows.append({"study": k, "last_checked_utc": last,
                     "state": "queued" if last in ("", "nan") else "checked"})
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "queue.csv"), index=False, encoding="utf-8-sig")
    state = {r["study"]: {"status": r["state"], "last_checked": r["last_checked_utc"]}
             for r in rows}
    json.dump(state, open(os.path.join(OUT, "state.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    print(f"missing datasets on record : {len(keys)}")
    print(f"never re-checked           : {sum(1 for r in rows if r['state'] == 'queued')}")
    print(f"re-checked at least once   : {sum(1 for r in rows if r['state'] == 'checked')}")
    print(f"queue -> {os.path.relpath(os.path.join(OUT, 'queue.csv'), B)}")


def mark(key, value, url=""):
    att = pd.read_csv(ATT) if os.path.isfile(ATT) else pd.DataFrame(columns=["study"])
    if "last_checked_utc" not in att.columns:
        att["last_checked_utc"] = ""
    m = att["study"].astype(str) == key
    if not m.any():
        att = pd.concat([att, pd.DataFrame([{"study": key}])], ignore_index=True)
        m = att["study"].astype(str) == key
    att.loc[m, "last_checked_utc"] = NOW
    if url:
        att.loc[m, "data_url"] = url
    att.to_csv(ATT, index=False, encoding="utf-8-sig")
    print(f"stamped {key} -> {NOW}" + (f" ({url})" if url else ""))


def status():
    q = pd.read_csv(os.path.join(OUT, "queue.csv")) if os.path.isfile(
        os.path.join(OUT, "queue.csv")) else pd.DataFrame()
    print(f"queue {len(q)} | " + q["state"].value_counts().to_dict().__str__() if len(q) else "no queue")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "--plan":
        plan()
    elif a[0] == "--mark" and len(a) >= 3:
        mark(a[1], a[2], a[3] if len(a) > 3 else "")
    elif a[0] == "--status":
        status()
    else:
        print(__doc__)
