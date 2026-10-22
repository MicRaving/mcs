"""The AGENTS.md a project carries.

A project folder is meant to be handed to someone else - a colleague, a script,
an agent - without a briefing.  The application writes this file when a project
is created, and the Settings view edits it afterwards; it describes the folder,
the scheme the project codes against and the rules that keep the corpus valid.

The text is deliberately about *this project*, not about the app: the app's own
documentation lives in the Home view and in ``Schemes/<id>/bundles/``.
"""

import os

DOC_NAME = "AGENTS.md"


def _scheme_facts(root):
    """The active scheme's id and the project's own manifest."""
    facts = {"id": "", "label": "", "name": "", "description": ""}
    try:
        import project_context

        facts["id"] = project_context.active_scheme_id(root) or ""
        data = project_context.manifest(root) or {}
        facts["label"] = str(data.get("label") or "")
        facts["name"] = str(data.get("name") or "")
        facts["description"] = str(data.get("description") or "")
    except Exception:  # noqa: BLE001 - a project without a scheme still gets a doc
        pass
    return facts

def template(root, project_name=""):
    """The AGENTS.md text for the project at ``root``."""
    facts = _scheme_facts(root)
    name = project_name or os.path.basename(root.rstrip("\\/")) or "this project"
    scheme_line = (
        f"This project codes against the scheme **{facts['label'] or facts['id']}** "
        f"(`{facts['id']}`), which lives beside the application in "
        f"`Schemes/{facts['id']}/` — not in this folder."
        if facts["id"] else
        "This project names its coding scheme in `Project/project.json`; the "
        "scheme itself lives beside the application in `Schemes/<id>/`."
    )
    description = facts["description"].strip()
    if description:
        description = "\n" + description + "\n"

    return f"""# AGENTS.md — {name}

This folder is a CodingStudio project. Everything below is the project's own
data: the application reads it and writes to it, but never owns it. An agent
working here should follow the conventions rather than invent its own.
{description}
## What is in this folder

| Folder / file | Contents |
|---|---|
| `Papers/` | one PDF per study, named after the study's key |
| `Text/` | the PDF's text, same name, `.txt` — used by search and the text view |
| `Extraction/` | the **reference** coding, `<Key>.json`, one object per experiment |
| `Codings/` | one file per person who coded: `<Key> - <Initials>.json` |
| `Screening/` | abstract decisions, one file per screener |
| `Datasets/` | a study's public data, its `harmonizer.py` and the outputs |
| `Reproduction/` | `<Key>.md` — what was re-run and whether the numbers came back |
| `Supplements/` | the paper's supplementary material |
| `Project/project.json` | this project's name and the scheme it uses |
| `Results/` | every product the analysis writes |
| `master_dataset.xlsx` | the screening workbook (abstracts and full-text verdicts) |
| `temp/` | intermediates; never evidence |

A study's **key** is its file name everywhere — the PDF, its text, its reference
coding, everyone's coding, its dataset folder. `Surname et al. (Year)` is the
convention; the only hard rule is that the same string is used in every folder.

## The scheme

{scheme_line}

The scheme defines the coding form: its blocks, fields, levels (`kinds`), the
conditions under which a field appears, and the screening criteria. Its bundle
holds the machine-readable copies an agent should use:

* `Schemes/<id>/bundles/scheme.json` — the scheme itself
* `Schemes/<id>/bundles/json-schema.json` — a JSON Schema for one coding record
* `Schemes/<id>/bundles/prompt.md` — the prompt to code with
* `Schemes/<id>/bundles/screening-prompt.md` — the prompt to screen with
* `Schemes/<id>/bundles/criteria.json` — the screening criteria

## The workflow

1. **Screen the abstract** (`Screening/`) against the scheme's criteria.
2. **Screen the full text** — `Fulltext_Exclusion_Criterion` at the top of the
   coding form; leave it empty when every criterion is met.
3. **Code the paper** (`Codings/<Key> - <Initials>.json`): experiments, arms,
   measures, one effect size per contrast.
4. **Let a second person code it**; their file is separate and never merged
   silently.
5. **Compare** the codings and settle what differs (the reference coding in
   `Extraction/` is the canonical one).
6. **Harmonise** the studies that have public data (`Datasets/<Key>/`).
7. **Reproduce** each study's own numbers (`Reproduction/<Key>.md`).
8. **Pool** the eligible contrasts (`Results/`).

## Rules that keep the corpus valid

* **Never write `Extraction/<Key>.json` from a coding session.** It is the
  reference coding; codings go to `Codings/` under the coder's initials.
* **Never invent a number.** An effect is either read from the paper (with a
  marked source) or the row carries a non-empty `missing_metrics_reason` naming
  the paragraph or table that was searched.
* **One contrast, one row.** A multi-df omnibus (`F` with `df_num > 1`, a
  `chi²`, an `η²`) is never the eligible effect: it cannot be signed and cannot
  become one two-arm `d`.
* **Every DV carries `contrast_type` and `eligible_primary`.** `eligible_primary
  == 1` means "this is the intervention-versus-control contrast the analysis may
  pool".
* **`Fulltext_Exclusion_Criterion`** carries only `C1`–`C4` or nothing.
* **Direction is recorded.** A measure that is better when lower is coded as
  such, so the analysis can flip it instead of guessing.
* **Leave a field empty when the paper does not state it.** An empty value can
  mean "not stated"; `0` cannot.

## Where to read more

The application's Home view carries the full documentation (getting started,
coding, screening, the scheme, agreement, harmonization, reproduction, the
analysis, a glossary). This file is the summary an agent needs before touching
the folder; those are the detail.
"""


def path_for(root):
    return os.path.join(root, DOC_NAME)


def read(root):
    """The project's AGENTS.md, or "" when it has none."""
    try:
        with open(path_for(root), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def write(root, text):
    """Write the project's AGENTS.md (never outside ``root``)."""
    path = path_for(root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path


def ensure(root, project_name=""):
    """Write the template when the project has no AGENTS.md yet."""
    if os.path.isfile(path_for(root)):
        return False
    write(root, template(root, project_name))
    return True
