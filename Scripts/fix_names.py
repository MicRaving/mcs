"""Rename misnamed study artifacts to the canonical `Surname et al. (Year)` convention."""
import glob
import os
import shutil
import sys

B = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRY = "--dry-run" in sys.argv

RENAME = {
    "Catharine Lynn Reznicek (2021)": "Reznicek (2021)",
    "Anne Zappi Williams (2022)": "Williams (2022)",
    "Hui Zhang et al. (2024)": "Zhang et al. (2024)",
    "HYUNYI CHO et al. (2018)": "Cho et al. (2018)",
    "Jannie Lilja et al. (2025)": "Lilja et al. (2025)",
    "Jennifer Zarzosa & Cecilia Ruvalcaba (2025)": "Zarzosa & Ruvalcaba (2025)",
    "Kelly Y. L. Ku et al. (2025)": "Ku et al. (2025 b)",
    "Lorien C. Abroms et al. (2024)": "Abroms et al. (2024)",
    "Lydia P. Gleaves & David A. Broniatowski (2024)": "Gleaves & Broniatowski (2024)",
    "M. Scull et al. (2019)": "Scull et al. (2019)",
    "Noguera - Sepulveda (2019)": "Noguera-Sepulveda (2019)",
    "Sevilay Aydemir & Ozden Demirkan (2024)": "Aydemir & Demirkan (2024)",
    "Trenton W. Ford et al. (2023)": "Ford et al. (2023)",
    "Yaseen Ali Azi et al. (2024)": "Azi et al. (2024)",
    "Fendt2026": "Fendt et al. (2026 a)",
    "FendtFriction_Exp1": "Fendt et al. (2026 b) Exp1",
    "FendtFriction_Exp2": "Fendt et al. (2026 b) Exp2",
    "Lyons et al. (2024) - A Health Media Literacy Intervention Increases Skepticism of Both Inaccurate and": "Lyons et al. (2024)",
    "Swire-Thompson et al. (2024) - Discrediting Health Disinformation Sources- Advantages of Highlighting Low Exper": "Swire-Thompson et al. (2024)",
    "TULIN et al. (2025)": "Tulin et al. (2025 a)",
    "Tulin et al. (2025) - Beyond Belief Correction": "Tulin et al. (2025 b)",
    "ARUGUETE & CALVO (2024)": "Aruguete & Calvo (2024)",
    "Abascal Miguel (2023)": "Abascal (2023)",
    "Austin EW et al. (2006)": "Austin et al. (2006)",
    "Farhad Seraji et al. (2025)": "Seraji et al. (2025)",
    "Ullrich K. H. Ecker et al. (2024)": "Ecker et al. (2024)",
    "Butler & Swire\u2010Thompson (2026)": "Butler & Swire-Thompson (2026)",
    "Dutta\u2010Powell et al. (2024)": "Dutta-Powell et al. (2024)",
    # UNCODED 2026-09-20: the fifth Fendt-2026 work (Analytic Thinking x Lateral
    # Reading, unpublished manuscript) uses the canonical collision form.
    "Fendt, Martin & Pennycook (2026)": "Fendt et al. (2026 x)",
}
DUPLICATES = {
    "Unknown (2017)",             # duplicate of Austin et al. (2018) (same DOI)
    "Ines Deibl et al. (2024)",   # duplicate of Deibl et al. (2024) (same DOI); Text copy
}
# Exact relative-path moves that are not simple `Surname (Year).ext` renames.
# UNCODED 2026-09-20: the stray-suffixed conference slide deck belongs in
# `Supplements/` (the canonical supplementary-file location), not `Papers/`.
SPECIAL_MOVES = {
    "Papers/Fendt et al. (2026 c) - Presentation.pdf":
        "Supplements/Fendt et al. (2026 c) Supplement 1.pdf",
}


def mv(src, dst):
    if os.path.abspath(src) == os.path.abspath(dst):
        return False
    if DRY:
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return False
    shutil.move(src, dst)
    return True


def main():
    n = 0
    # exact relative-path special moves (stray suffixes, supplements, ...)
    for src_rel, dst_rel in SPECIAL_MOVES.items():
        src = os.path.join(B, src_rel)
        dst = os.path.join(B, dst_rel)
        if os.path.exists(src) and mv(src, dst):
            n += 1
            print("REN ", src_rel, "->", dst_rel)
    # simple files: Extraction(.json/.md), Text(.txt), Reproduction(.md), Papers(.pdf)
    for folder in ["Extraction", "Text", "Reproduction", "Papers"]:
        for p in glob.glob(os.path.join(B, folder, "*")):
            stem, ext = os.path.splitext(os.path.basename(p))
            if stem in RENAME:
                if mv(p, os.path.join(B, folder, RENAME[stem] + ext)):
                    n += 1
                    print("REN ", folder, os.path.basename(p), "->", RENAME[stem] + ext)
            elif stem in DUPLICATES:
                dst = os.path.join(B, "archive", "_duplicates", os.path.basename(p))
                if mv(p, dst):
                    n += 1
                    print("DUP ", folder, os.path.basename(p), "-> archive/_duplicates/")
    # Datasets folders: rename folder and every file inside with the old prefix
    for d in glob.glob(os.path.join(B, "Datasets", "*")):
        if not os.path.isdir(d):
            continue
        base = os.path.basename(d)
        new = RENAME.get(base)
        if new:
            for p in glob.glob(os.path.join(d, base + " - *")):
                inner = os.path.basename(p).replace(base + " - ", new + " - ", 1)
                if mv(p, os.path.join(d, inner)):
                    n += 1
            if mv(d, os.path.join(B, "Datasets", new)):
                n += 1
                print("REN Datasets/", base, "->", new)
        elif base in DUPLICATES:
            if mv(d, os.path.join(B, "archive", "_duplicates", base)):
                n += 1
    print("renames/moves:", n)


if __name__ == "__main__":
    sys.exit(main())
