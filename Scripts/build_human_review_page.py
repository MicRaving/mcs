"""Build a navigable review sheet for the human master's queue.

``Results/human_decisions_review.csv`` is the machine-readable sheet and
``Results/HUMAN_DECISIONS_DRAFT.md`` is the long-form evidence dump.  With
a few thousand items neither is readable, so this writes a single
self-contained HTML page: filter by field, by study key, by decision, or
by a free-text search; read the competing values, the proposed value and
the evidence on one line; mark each item approve / override / hold; and
export the filled sheet back to CSV.

The page reads only the products - it never touches the corpus.
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "Results")
REVIEW = os.path.join(RESULTS, "human_decisions_review.csv")
OUT_DIR = os.path.join(RESULTS, "human_review")
OUT = os.path.join(OUT_DIR, "index.html")

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Human master review sheet</title>
<style>
  :root {
    --bg: #ffffff; --fg: #16181d; --muted: #6b7280; --line: #e5e7eb;
    --card: #f7f8fa; --accent: #2563eb; --ok: #047857; --warn: #b45309;
    --bad: #b91c1c;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14161a; --fg:#e7e9ee; --muted:#9aa3b2; --line:#2a2e36;
            --card:#1b1e24; --accent:#7aa2f7; --ok:#34d399; --warn:#fbbf24;
            --bad:#f87171; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  header { padding:18px 22px; border-bottom:1px solid var(--line);
           position:sticky; top:0; background:var(--bg); z-index:5; }
  h1 { font-size:1.15rem; margin:0 0 10px; letter-spacing:-.01em; }
  .bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  select, input, button { font:inherit; color:inherit; background:var(--bg);
    border:1px solid var(--line); border-radius:7px; padding:5px 8px; }
  input[type=search] { min-width:260px; }
  button { cursor:pointer; }
  button.primary { background:var(--accent); color:#fff; border-color:transparent; }
  .counts { color:var(--muted); font-size:.85rem; margin-top:8px; }
  .counts b { color:var(--fg); }
  main { padding:14px 22px 60px; }
  .grp { border-bottom:1px solid var(--line); padding:10px 0 4px; }
  .grp h2 { font-size:.85rem; text-transform:uppercase; letter-spacing:.05em;
            color:var(--muted); margin:12px 0 6px; font-weight:700; }
  table { width:100%; border-collapse:collapse; font-size:.87rem; }
  th { text-align:left; font-weight:600; color:var(--muted); font-size:.74rem;
       text-transform:uppercase; letter-spacing:.04em; padding:5px 6px;
       position:sticky; top:0; background:var(--bg); }
  td { padding:7px 6px; vertical-align:top; border-top:1px solid var(--line); }
  tr:hover td { background:var(--card); }
  .key { font-weight:600; }
  .sub { color:var(--muted); font-size:.8rem; }
  .ev { color:var(--muted); font-size:.82rem; max-width:60ch; }
  .q { color:var(--muted); font-size:.78rem; font-style:italic; max-width:50ch; }
  .pill { display:inline-block; padding:1px 7px; border-radius:999px;
          font-size:.72rem; font-weight:700; }
  .c-clear { background:color-mix(in srgb, var(--ok) 18%, transparent); color:var(--ok); }
  .c-conditional { background:color-mix(in srgb, var(--warn) 20%, transparent); color:var(--warn); }
  .c-none, .c-keep { background:color-mix(in srgb, var(--muted) 18%, transparent); color:var(--muted); }
  .dec { min-width:96px; }
  .val { min-width:110px; }
  .set { color:var(--ok); font-weight:700; }
  .todo { color:var(--warn); }
  .empty { padding:40px 0; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>Human master review sheet</h1>
  <div class="bar">
    <select id="f-field"><option value="">every field</option></select>
    <select id="f-key"><option value="">every study</option></select>
    <select id="f-conf">
      <option value="">every confidence</option>
      <option value="clear">clear</option>
      <option value="conditional">conditional</option>
      <option value="none">no basis</option>
      <option value="keep">artefact - keep canonical</option>
    </select>
    <select id="f-dec">
      <option value="">every decision</option>
      <option value="approve">approve</option>
      <option value="override">override</option>
      <option value="hold">hold</option>
      <option value="__none">not yet marked</option>
    </select>
    <input id="f-text" type="search" placeholder="search key, outcome, evidence, quote">
    <button id="btn-all-approve">approve every clear item</button>
    <button id="btn-export" class="primary">export CSV</button>
  </div>
  <div class="counts" id="counts"></div>
</header>
<main><div id="out"></div></main>
<script>
const ITEMS = __DATA__;
const out = document.getElementById('out');
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const fField = document.getElementById('f-field');
const fKey   = document.getElementById('f-key');
const fConf  = document.getElementById('f-conf');
const fDec   = document.getElementById('f-dec');
const fText  = document.getElementById('f-text');

const fields = [...new Set(ITEMS.map(i=>i.field))].sort();
const keys   = [...new Set(ITEMS.map(i=>i.study_key))].sort();
fields.forEach(f => fField.insertAdjacentHTML('beforeend', `<option value="${esc(f)}">${esc(f)}</option>`));
keys.forEach(k  => fKey.insertAdjacentHTML('beforeend',   `<option value="${esc(k)}">${esc(k)}</option>`));

function visible() {
  const t = fText.value.trim().toLowerCase();
  return ITEMS.filter(i => {
    if (fField.value && i.field !== fField.value) return false;
    if (fKey.value   && i.study_key !== fKey.value) return false;
    if (fConf.value  && i.confidence !== fConf.value) return false;
    if (fDec.value) {
      const d = i.decision || '';
      if (fDec.value === '__none') { if (d) return false; }
      else if (d !== fDec.value) return false;
    }
    if (t) {
      const hay = (i.study_key+' '+i.dv+' '+i.field+' '+i.competing_values+' '+
                   i.proposed+' '+i.evidence+' '+i.quote).toLowerCase();
      if (!hay.includes(t)) return false;
    }
    return true;
  });
}

function render() {
  const rows = visible();
  const decided = rows.filter(i=>i.decision).length;
  document.getElementById('counts').innerHTML =
    `<b>${rows.length}</b> item(s) shown &middot; <b>${decided}</b> marked &middot; ` +
    `<b>${ITEMS.length}</b> in the queue &middot; ` +
    `<b>${ITEMS.filter(i=>i.decision==='approve').length}</b> approved, ` +
    `<b>${ITEMS.filter(i=>i.decision==='override').length}</b> overridden, ` +
    `<b>${ITEMS.filter(i=>i.decision==='hold').length}</b> held`;
  if (!rows.length) { out.innerHTML = '<div class="empty">nothing matches these filters</div>'; return; }
  const byField = {};
  rows.forEach(i => (byField[i.field] = byField[i.field] || []).push(i));
  let html = '';
  for (const f of Object.keys(byField).sort()) {
    html += `<div class="grp"><h2>${esc(f)} &mdash; ${byField[f].length} item(s)</h2><table>
      <thead><tr><th style="width:17%">study / outcome</th><th style="width:13%">competing values</th>
      <th style="width:11%">proposed</th><th>evidence</th><th style="width:9%">mark</th>
      <th style="width:9%">value to set</th></tr></thead><tbody>`;
    for (const i of byField[f]) {
      html += `<tr>
        <td><div class="key">${esc(i.study_key)}</div><div class="sub">${esc(i.dv||'(record level)')}</div></td>
        <td class="sub">${esc(i.competing_values)}</td>
        <td><span class="set">${esc(i.proposed_value||'-')}</span>
            <div class="sub"><span class="pill c-${esc(i.confidence)}">${esc(i.confidence)}</span></div></td>
        <td><div class="ev">${esc(i.evidence)}</div>${i.quote?`<div class="q">&ldquo;${esc(i.quote)}&rdquo;</div>`:''}</td>
        <td><select class="dec" data-id="${i.id}">
          <option value="">-</option><option value="approve">approve</option>
          <option value="override">override</option><option value="hold">hold</option></select></td>
        <td><input class="val" data-id="${i.id}" value="${esc(i.value||'')}" placeholder="${esc(i.proposed_value||'')}"></td>
      </tr>`;
    }
    html += '</tbody></table></div>';
  }
  out.innerHTML = html;
  out.querySelectorAll('select.dec').forEach(s => {
    const it = ITEMS.find(x=>x.id===+s.dataset.id);
    s.value = it.decision || '';
    s.onchange = () => { it.decision = s.value; render(); };
  });
  out.querySelectorAll('input.val').forEach(v => {
    const it = ITEMS.find(x=>x.id===+v.dataset.id);
    v.oninput = () => { it.value = v.value; };
  });
}

document.getElementById('btn-all-approve').onclick = () => {
  ITEMS.forEach(i => { if (i.confidence === 'clear' && !i.decision) { i.decision='approve'; i.value = i.proposed_value; } });
  render();
};
document.getElementById('btn-export').onclick = () => {
  const head = ['study_key','field','dv','competing_values','proposed','proposed_value',
                'confidence','evidence','quote','has_evidence','decision','value','reviewer_note'];
  const q = s => '"' + String(s==null?'':s).replace(/"/g,'""') + '"';
  const lines = [head.join(',')];
  ITEMS.forEach(i => head.forEach(h => lines.push('')));
  const rows = ITEMS.map(i => head.map(h => q(i[h])).join(','));
  const blob = new Blob([lines[0] + '\\n' + rows.join('\\n') + '\\n'],
                         {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'human_decisions_review_filled.csv';
  a.click();
};
[fField,fKey,fConf,fDec].forEach(e => e.onchange = render);
fText.oninput = render;
fText.onkeyup = render;
fText.onchange = render;
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.activeElement === fText) { e.preventDefault(); render(); }
});
render();
</script>
</body>
</html>
"""


def main():
    if not os.path.isfile(REVIEW):
        print("no review sheet: %s" % REVIEW, file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(REVIEW, encoding="utf-8-sig", newline="")))
    items = []
    for i, r in enumerate(rows):
        items.append({
            "id": i,
            "study_key": r.get("study_key", ""),
            "field": r.get("field", ""),
            "dv": r.get("dv", ""),
            "competing_values": r.get("competing_values", "")[:300],
            "proposed": (r.get("proposed") or "")[:600],
            "proposed_value": r.get("proposed_value", ""),
            "confidence": r.get("confidence", ""),
            "evidence": (r.get("evidence") or "")[:900],
            "quote": (r.get("quote") or "")[:500],
            "has_evidence": r.get("has_evidence", ""),
            "decision": (r.get("decision") or "").strip(),
            "value": r.get("value", ""),
            "reviewer_note": r.get("reviewer_note", ""),
        })
    html = PAGE.replace("__DATA__", json.dumps(items, ensure_ascii=False))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("wrote %s (%d items, %.1f MB)"
          % (OUT, len(items), len(html) / 1e6))
    print("fields:", dict(Counter(i["field"] for i in items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
