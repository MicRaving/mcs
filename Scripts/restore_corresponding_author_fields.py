"""Restore the corresponding-author fields on the papers a restore overwrote.

The reconciliation backup was taken while the last corresponding-author
agent was still running, so restoring it dropped that agent's writes on the
five papers it had not reached yet.  Their findings are recorded here, so
the fields can be re-applied without re-reading the PDFs.

Run:  python Scripts/restore_corresponding_author_fields.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACT = os.path.join(ROOT, "Extraction")

#: key -> (name, email, source) as the agent recorded them
RESTORE = {
    "Jones (2023)": (
        None, None,
        "paper: no corresponding author named (UC Merced MS thesis front "
        "matter, PDF pp. 1-8: author, title, committee and copyright only; "
        "no correspondence note and no e-mail printed anywhere in the PDF)"),
    "Khandoozi (2023)": (
        None, None,
        "paper: no corresponding author named (Queen's University PhD thesis "
        "front matter, PDF pp. 1-12: title, co-authorship statement and "
        "acknowledgements only; no correspondence note and no e-mail printed "
        "anywhere in the PDF)"),
    "Kurt (2024)": (
        None, None,
        "paper: no corresponding author named (Boston University PhD "
        "dissertation front matter, PDF pp. 1-9: repository cover, title page, "
        "copyright, approval page, acknowledgements, abstract and contents "
        "only; no correspondence note and no e-mail printed anywhere in the "
        "PDF)"),
    "LaCaille & Hessler (2021)": (
        None, None,
        "paper: no corresponding author named (book chapter, PDF pp. 1-9: "
        "chapter title page with authors, affiliation, abstract and body text "
        "only; no author note, correspondence note or e-mail printed in the "
        "PDF)"),
    "Lutzke (2019)": (
        None, None,
        "paper: no corresponding author named (working paper, PDF p. 1: sole "
        "author and affiliation only; no correspondence note and no e-mail "
        "printed anywhere in the PDF)"),
}


def main():
    for key, (name, email, source) in RESTORE.items():
        path = os.path.join(EXTRACT, key + ".json")
        if not os.path.isfile(path):
            print("missing:", path)
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        recs = data if isinstance(data, list) else data.get("experiments", [])
        for rec in recs:
            rec["Corresponding_Author_Name"] = name
            rec["Corresponding_Author_Email"] = email
            rec["Corresponding_Author_Source"] = source
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("restored %-28s records=%d" % (key, len(recs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
