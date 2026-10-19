"""Audit artifact names against the canonical `Surname et al. (Year)` convention."""
import glob
import os
import re
import sys

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:  # print real UTF-8 names (a Windows console otherwise renders accented
    # letters as U+FFFD, which reads as if the file names were mojibake)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# A canonical key is
#   `Surname[ Surname...][ et al.][ & Surname[ Surname...]] (Year[ <letter>])`.
# Compound (multi-token) surnames and any lowercase collision suffix are
# legitimate; particle-led keys (`van ...`, `von ...`, `de ...`) remain flagged
# for the human reviewer.
_NAME = r"[A-Z\u00c0-\u017f][\w'\u2019\-\u00c0-\u017f]*"
GOOD = re.compile(
    r"^" + _NAME + r"(?: " + _NAME + r")*"
    r"(?: et al\.)?"
    r"(?: & " + _NAME + r"(?: " + _NAME + r")*)?"
    r" \(\d{4}(?: [a-z])?\)$")

SEV = re.compile(r"([A-Z]\.\s)|(\b(von|van|de|der)\b)|( - )|(TITLE::)")

# Non-study infrastructure directories that live beside the Datasets/<Key>/ folders.
INFRA = {"_meta_analyses", "temp"}


def items_for(folder, ext):
    if ext:
        return [os.path.splitext(os.path.basename(p))[0]
                for p in glob.glob(os.path.join(B, folder, "*" + ext))]
    return [d for d in os.listdir(os.path.join(B, folder))
            if os.path.isdir(os.path.join(B, folder, d)) and d not in INFRA]


def main():
    for folder, ext in [("Extraction", ".json"), ("Datasets", None), ("Papers", ".pdf"), ("Text", ".txt")]:
        items = items_for(folder, ext)
        bad = [i for i in items if not GOOD.match(i)]
        print(f"== {folder}: {len(items)} items | {len(bad)} non-canonical")
        for x in sorted(bad):
            print("   ", x)


if __name__ == "__main__":
    sys.exit(main())
