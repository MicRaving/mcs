"""Build batched prompts for the corresponding-author resolution.

The 78 papers the harvest could not resolve are given to coding agents in
batches: every key in a batch lives in a DIFFERENT file, so the agents never
write to the same file at the same time.

Writes ``temp/mm_route/corr_batches/<NN>.txt`` (one prompt per batch).
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Scripts"))
import corresponding_author_queue as Q  # noqa: E402

BATCH = int(os.environ.get("CORR_BATCH", "4"))

HEAD = """You are resolving the corresponding author for {n} papers.

For EACH paper below, read its PDF, find the correspondence note, and write
THREE record-level fields into its canonical Extraction file:
  "Corresponding_Author_Name":  the person's name, e.g. "Jane Roe"   (or null)
  "Corresponding_Author_Email": the address, e.g. "jroe@univ.edu"     (or null)
  "Corresponding_Author_Source": where you read it, e.g.
      "pdf: correspondence note, PDF p. 1" / "pdf: author footnote, PDF p. 3" /
      "openalex: corresponding_author_ids" / "paper: no corresponding author named"

The Extraction file is a JSON array of experiment records; set the same three
fields on EVERY record in the array.

RULES
- The name must be one of the paper's own authors (or a person the paper
  explicitly directs correspondence to). Never use an editor, a production
  office, a web-master or a general departmental mailbox unless the paper
  really does direct correspondence there.
- The e-mail must be printed in the paper. Do NOT invent an address and do
  NOT guess one from a domain pattern. If the paper prints no address, set the
  e-mail to null and say so in the source field.
- If the paper names no corresponding author, set all three to null with a
  source like "paper: no corresponding author named".
- Keep every other key in the file EXACTLY as it is. Do not reformat, reorder
  or "fix" anything else. Do not touch any paper outside your batch.
- Read only the listed papers' PDF/Text. Do NOT read any Extraction/*.json for
  a key outside your batch, any Codings/ file, or any Datasets/ or
  Reproduction/ artefact.
- Never delete anything under temp/; leave your scratch in place.

VERIFY before finishing (repeat for every key in the batch):
  python -c "import json;d=json.load(open('Extraction/<KEY>.json',encoding='utf-8'));r=d if isinstance(d,list) else d['experiments'];print(len(r),[(x.get('Corresponding_Author_Name'),x.get('Corresponding_Author_Email'),x.get('Corresponding_Author_Source')) for x in r][:1])"
It must print the key's record count and your three values.

Scratch under temp/mm_route/corr/ only; keep it (do not delete other folders).

Reply with one line per paper: key | name | email | source | page.
"""


def main():
    todo = json.load(open(os.path.join(ROOT, "temp", "mm_route", "corr_todo.json"),
                         encoding="utf-8"))
    outdir = os.path.join(ROOT, "temp", "mm_route", "corr_batches")
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        parts = [HEAD.format(n=len(chunk))]
        for k in chunk:
            d = json.load(open(os.path.join(ROOT, "Extraction", k + ".json"),
                              encoding="utf-8"))
            r = d if isinstance(d, list) else d.get("experiments", [])
            auth = str(r[0].get("Authors", ""))[:400] if r else "(unknown)"
            parts.append("-" * 60)
            parts.append("Study key: %s" % k)
            parts.append("Sources : Papers/%s.pdf , Text/%s.txt" % (k, k))
            parts.append("Authors : %s" % auth)
            parts.append("File    : Extraction/%s.json  (write the 3 fields on every record)" % k)
        path = os.path.join(outdir, "%02d.txt" % i)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts))
        n += 1
    print("wrote %d batch prompts (batch size %d) to %s" % (n, BATCH, outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
