"""Dispatch the papers whose corresponding author could not be harvested.

Reads ``Results/corresponding_author_log.csv``, keeps the rows with no
``resolved`` flag, and prints one prompt per paper for the coding agents.
The agents read the PDF, find the correspondence note, and write the three
fields into ``Extraction/<Key>.json``.

Usage::

    python Scripts/code_corresponding_author.py --queue
"""
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "Results", "corresponding_author_log.csv")

TEMPLATE = """You are resolving the corresponding author for one paper.

Study key: {key}
Sources: Papers/{key}.pdf and Text/{key}.txt
Already-known authors (canonical Authors field): {authors}

TASK
Find the paper's corresponding author and write THREE record-level fields into
Extraction/{key}.json (the file is a JSON array of experiment records; set the
same three fields on EVERY record in the array):
  "Corresponding_Author_Name":  the person's name, e.g. "Jane Roe"   (or null)
  "Corresponding_Author_Email": the address, e.g. "jroe@univ.edu"     (or null)
  "Corresponding_Author_Source": where you read it, e.g.
      "pdf: correspondence note, PDF p. 1" / "pdf: author footnote, PDF p. 3" /
      "openalex: corresponding_author_ids" / "paper: no corresponding author named"

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
  or "fix" anything else. Do not touch any other paper's file.
- Read only this paper's PDF/Text and the shared schema. Do NOT read any
  Extraction/*.json for OTHER keys, any Codings/ file, or any Datasets/ or
  Reproduction/ artefact.

VERIFY before finishing:
  python -c "import json;d=json.load(open('Extraction/{key}.json',encoding='utf-8'));r=d if isinstance(d,list) else d['experiments'];print(len(r),[ (x.get('Corresponding_Author_Name'), x.get('Corresponding_Author_Email'), x.get('Corresponding_Author_Source')) for x in r][:1])"
It must print the key's record count and your three values.

Scratch under temp/mm_route/corr/{key}/ only; delete it when done.
Reply with: key, name, email, source, and the page you read it from.
"""


def main():
    if not os.path.isfile(LOG):
        print("no log; run code_corresponding_author.py first", file=sys.stderr)
        return 1
    rows = list(csv.DictReader(open(LOG, encoding="utf-8-sig")))
    todo = [r for r in rows if r.get("resolved") != "1"]
    # the canonical Authors string, for the prompt
    out = []
    for r in todo:
        key = r["study_key"]
        authors = ""
        try:
            d = json.load(open(os.path.join(ROOT, "Extraction", key + ".json"),
                              encoding="utf-8"))
            recs = d if isinstance(d, list) else d.get("experiments", [])
            if recs:
                authors = str(recs[0].get("Authors", ""))[:400]
        except Exception:
            pass
        out.append(TEMPLATE.format(key=key, authors=authors or "(unknown)"))
    print("unresolved:", len(out))
    for p in out:
        print("=" * 70)
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
