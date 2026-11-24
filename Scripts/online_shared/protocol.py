"""Protocol constants and file classification for online projects.

The module is shared by the desktop client and the studio server and is
therefore **stdlib only** (``re``, ``hashlib``, ``dataclasses`` ... nothing
else).

``classify(path)`` answers one question: *what kind of project file is this,
and who may change it?*  Every consumer (push permission checks, the mirror
scan, the core/non-core split of the cache) reads that answer from here, so
the rule table lives in exactly one place.

Paths are **POSIX-style and relative to the project root**.  Backslashes are
accepted and normalised (the desktop app runs on Windows), as is a leading
``./``.  Matching is case-sensitive: the app writes lower-case extensions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# --- §1 constants ---------------------------------------------------------

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8770

HEARTBEAT_SECS = 15          # presence heartbeat
PRESENCE_TTL_SECS = 300
SYNC_INTERVAL_SECS = 20      # client poll/push cadence when online
PUSH_DEBOUNCE_SECS = 5       # coalesce rapid saves of one path
DEFAULT_LOCK_TTL_H = 24
DEFAULT_MAX_OFFLINE_LEASE_H = 72
DEFAULT_CACHE_MB = 1000

STAGES = ("abstract", "fulltext", "coding")
TASKS = ("abstract", "fulltext", "coding", "settle", "harmonize", "reproduce")
ROLES = ("owner", "coder", "viewer")          # project member roles

KINDS = ("excluded", "owned", "shared", "admin", "derived", "immutable")

#: Paths that are always mirrored into a client's working copy.  Note that
#: ``Text/`` is in the list even though its files are *immutable*: they are
#: small text and every workflow reads them.  ``Papers/`` and ``Supplements/``
#: stay immutable and non-core (they are fetched on demand).
CORE_PREFIXES = (
    "Project/",
    "Codings/",
    "Extraction/",
    "Screening/",
    "Fulltext/",
    "Text/",
    "Reproduction/",
)

CORE_ROOT_FILES = ("master_dataset.xlsx",)

#: Coder tags of the **AI coding routes** - the independent reliability routes
#: that re-code the corpus (AGENTS.md): SB = Space Bunny
#: (``opencode-go/space-bunny-free``), MM = MiniMax
#: (``opencode/minimax-m3.1-flash-free``), DS = DeepSeek.
#:
#: Their artefacts are ``Codings/<Key> - SB|MM|DS.json`` (+ ``.md``).  They are
#: ~264 MB - 43 % of a co-author's first-login download - and no coding workflow
#: needs them, so they are **on demand** in the core tier (``is_core`` False):
#: the app lists them from the manifest (``n_coders``, coder lists) and fetches
#: them only when a view that scores every route is opened (the agreement/IRR
#: tab, ``agreement_lib.ensure_rater_files``).  A pinned key still pulls them
#: (an explicit "take offline").
#:
#: The routes' *other* owned files stay core: a harmonizer/assignments draft
#: (``Codings/<Key> - harmonizer|assignments - <Tag>.json``) and every
#: screening/full-text decision file (``Screening/``, ``Fulltext/``) are small
#: and read by their own tabs.
AI_ROUTE_TAGS = frozenset({"SB", "MM", "DS"})

#: ``Datasets/<Key>/<Key> - <tail>`` CSV products that stay in the core tier.
#: They are the small metadata sidecars the listing/summary code reads without
#: opening a study (``variables.csv`` and ``harmonized_variables.csv`` are
#: walked for every study); every *other* dataset CSV product - ``harmonized``,
#: ``prepared``, the raw copies and the ``- SB`` twins - is fetched on demand
#: (INTERFACES 4).  ``.json``/``.md``/``.py`` dataset files are unchanged
#: (11.5 / 0.6 / 35 MB in the real project).
DATASET_CORE_CSVS = (
    "variable_assignments.csv",   # shared-exclusive: the coder's assignment file
    "variables.csv",              # provenance map (coverage, harmonizer context)
    "harmonized_variables.csv",   # sidecar: roles/polarity of every DV
    "dv_direction.csv",           # polarity sidecar (repro)
    "exclusions.csv",             # author exclusion/attention-check rules
    "conditions.csv",             # arm roles used by the SDT split
    "repro_results.csv",          # per-study reproduction rows
)


@dataclass(frozen=True)
class FileClass:
    """Classification of one project-relative path (see :func:`classify`)."""

    kind: str            # excluded|owned|shared|admin|derived|immutable
    key: str | None = None    # study key (owned coding files, shared files, Datasets)
    tag: str | None = None    # coder tag (owned files)
    stage: str | None = None  # abstract|fulltext|coding (owned files)
    task: str | None = None   # task needed to write a shared file
    #: Stages whose ``blinded`` setting hides this file (see ``visibility.can_read``).
    #: Empty for everything whose content is not another member's verdict.  A derived
    #: consensus file (the master workbook, settlements, the agreed full text) names
    #: every stage whose verdicts it encodes; it is hidden while *any* of them is
    #: blinded.
    blind_stages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown file kind: {self.kind!r}")
        for stage in self.blind_stages:
            if stage not in STAGES:
                raise ValueError(f"unknown blind stage: {stage!r}")


# --- rule table -----------------------------------------------------------

# Rule 1: never mirrored, never synced.
_EXCLUDED_DIR = re.compile(r"^(?:temp|archive|\.claude|\.studio|\.git|CodingStudio[^/]*)(?:/|$)")
_EXCLUDED_ANY = (
    _EXCLUDED_DIR,
    re.compile(r"(?:^|/)__pycache__/"),
    # build outputs and tool caches anywhere in the tree (the app build in
    # Scripts/coding_web/dist/ alone is ~530 MB of DLLs)
    re.compile(r"^Scripts/coding_web/(?:dist|build)(?:/|$)"),
    # the owner's separate metacognition strand: stays in the project folder
    # (its scripts read the project root) but is never synced (decision 2026-11-24)
    re.compile(r"^metacognition(?:/|$)"),
    re.compile(r"(?:^|/)(?:\.ruff_cache|\.pytest_cache|\.mypy_cache|\.openchamber)(?:/|$)"),
    re.compile(r"\.(?:exe|pyc)$"),
    re.compile(r"^~\$"),          # Office lock files: ~$report.docx
    re.compile(r"\.tmp$"),
)

# Rule 2/3: a coding file.  ``key`` is greedy so the tag is the LAST " - "
# segment of the file name — study titles may contain " - " themselves.
_CODING = re.compile(r"^Codings/(?P<key>.+) - (?P<tag>[^/]+)\.(json|md)$")
_CODING_ADMIN = re.compile(r"^Codings/.+ - _[^/]+\.(json|md)$")

# Rule 4/5: whole-stage screening/fulltext files, one per coder tag.
_CODING_HARM = re.compile(
    r"^Codings/(?P<key>.+?) - (?:harmonizer|assignments) - (?P<tag>[^/]+)\.json$")
_FULLTEXT_AGREED = re.compile(r"^Screening/fulltext_agreed\.json$")
_SCREENING_FT = re.compile(r"^Screening/fulltext - (?P<tag>[^/]+)\.json$")
_SCREENING = re.compile(r"^Screening/(?P<tag>[^/]+)\.json$")
_FULLTEXT = re.compile(r"^Fulltext/(?P<tag>[^/]+)\.json$")

# Rule 3b: the Master's settlement store (and its legacy forms) holds the
# abstract *and* full-text decisions on coder disagreements.
_SETTLEMENTS = re.compile(r"^Codings/_settlements\.json$")
_SETTLED_LOG = re.compile(r"^Codings/_settled\.jsonl$")
_RESOLUTIONS = re.compile(r"^Codings/_resolutions/")

# Rule 6-11: the settled artefacts and the datasets.
_EXTRACTION = re.compile(r"^Extraction/(?P<key>[^/]+)\.(json|md)$")
_REPRODUCTION = re.compile(r"^Reproduction/(?P<key>[^/]+)\.md$")
_DATASET_VAR_ASSIGN = re.compile(
    r"^Datasets/(?P<key>[^/]+)/(?P=key) - variable_assignments\.csv(?:\.gz)?$"
)
_DATASET_PRODUCT = re.compile(
    r"^Datasets/(?P<key>[^/]+)/(?P=key) - [^/]+\.(?:csv(?:\.gz)?|json|md)$"
)
_DATASET_CODE = re.compile(r"^Datasets/(?P<key>[^/]+)/[^/]+\.py$")
_DATASET_ANY = re.compile(r"^Datasets/(?P<key>[^/]+)/")

# Rule 12: sources.  The key is the file stem without the extension and
# without a trailing " Supplement N".
_IMMUTABLE = re.compile(r"^(?:Papers|Text|Supplements)/")
_SUPPLEMENT_SUFFIX = re.compile(r"(?i)\s+supplement\s+\d+$")
_LAST_EXT = re.compile(r"\.[^./]*$")

# Rule 13: analysis products.  The ones that compute *across* coders (agreement,
# disagreement, IRR, the coder reconciliation/queue files, the harvest routes and
# the Master briefings) encode every route's verdicts and are handled like a
# foreign-tag file (see ``blind_stages`` and ``visibility.can_read``).
_DERIVED = re.compile(r"^Results/")
_MASTER = re.compile(r"^master_dataset\.xlsx$")
_RESULTS_BLIND = re.compile(
    r"agreement|disagreement|irr|coder_|master_briefing|human_master_briefing"
    r"|master_agent_flags|polarity_divergence|polarity_reconciliation"
    r"|(?:^|/)sb_|(?:^|/)mm_|settle",
    re.IGNORECASE,
)

# Rule 13b: the owner's compile inputs under ``Project/``.  ``master_records`` is
# the decision-free record table the screening tab reads when the master
# workbook is hidden (a blinded coder): bibliographic columns only, never a
# verdict/stage/Screened_* column, so it is readable by every member and NEVER
# blinded.  ``master_base`` is the compiler's input and carries the curated
# stages (``Effective_Verdict``): it follows the master's blinding.
_PROJECT_RECORDS = re.compile(r"^Project/master_records\.(?:csv(?:\.gz)?|tsv(?:\.gz)?|jsonl|xlsx)$")
_PROJECT_BASE = re.compile(r"^Project/master_base\.xlsx$")

#: Rule 14: everything else is administrative.
#: (``Project/`` is admin, ``Screening/`` non-JSON is admin, unknown paths are admin.)


def normalize_path(path: str) -> str:
    """Return *path* as a POSIX relative path (backslashes, ``./`` removed)."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _immutable_key(path: str) -> str:
    """Study key of an immutable source file (``Papers/``, ``Text/``, ``Supplements/``).

    ``Smith (2020).pdf`` -> ``Smith (2020)`` and
    ``Smith (2020) Supplement 2.pdf`` -> ``Smith (2020)``.
    """
    base = path.rsplit("/", 1)[-1]
    stem = _LAST_EXT.sub("", base) or base
    return _SUPPLEMENT_SUFFIX.sub("", stem)


def classify(path: str) -> FileClass:
    """Classify a project-relative *path*; the first matching rule wins.

    The table (``Results/INTERFACES_online_projects.md`` §1.1):

    =  pattern                                                             kind
    =  ==================================================================  =========
    1  ``temp|archive|.claude|.studio|.git|CodingStudio*``, ``__pycache__``, excluded
       ``.exe``/``.pyc``, ``~$*``, ``*.tmp``
    2  ``Codings/<key> - <tag>.(json|md)`` (tag not starting ``_``)        owned
    3  ``Codings/<...> - _<tag>.(json|md)`` (e.g. ``_settlements``)        admin
    3b ``Codings/_settlements.json``, ``_settled.jsonl``,                 admin + blinded
       ``_resolutions/*`` (abstract + fulltext decisions)
    4  ``Screening/<tag>.json``                                            owned
    5  ``Fulltext/<tag>.json``                                             owned
    6  ``Extraction/<key>.(json|md)``                                      shared (settle)
    7  ``Reproduction/<key>.md``                                           shared (reproduce)
    8  ``Datasets/<K>/<K> - variable_assignments.csv``                     shared (harmonize)
    9  ``Datasets/<K>/<K> - <anything>.(csv[.gz]|json|md)``                 shared (reproduce)
    10 ``Datasets/<K>/<anything>.py``                                      shared (reproduce)
    11 ``Datasets/<K>/<anything else>``  (raw data)                        immutable
    12 ``Papers/``, ``Text/``, ``Supplements/``                            immutable
    13 ``Results/``, ``master_dataset.xlsx``                               derived
    13b ``Project/master_records.*`` (no verdict column)                    derived
    13b ``Project/master_base.xlsx``                                        admin + blinded
    14 ``Project/``, ``Screening/`` non-JSON, anything else                admin
    =  ==================================================================  =========

    ``key`` is the study key (``Surname et al. (Year)``) where the path names a
    study; ``tag`` is the coder tag of an owned file; ``stage`` is the stage an
    owned file belongs to; ``task`` is the task needed to write a shared file;
    ``blind_stages`` names the stages whose ``blinded`` setting hides the file
    (rule 3b, rule 4a, rule 13 - see ``visibility.can_read`` in 3.5).
    """
    p = normalize_path(path)

    # 1 — excluded
    if any(rx.search(p) for rx in _EXCLUDED_ANY):
        return FileClass("excluded")

    # 2a — a coder's harmonization drafts: "<Key> - harmonizer|assignments - <Tag>.json"
    #      (audit W4/W5/W9); the key must not swallow the " - harmonizer" infix.
    m = _CODING_HARM.match(p)
    if m is not None and not m.group("tag").startswith("_"):
        return FileClass("owned", key=m.group("key"), tag=m.group("tag"), stage="coding")

    # 2 — owned coding file (the tag is the last " - " segment, never "_..."
    #     because those are administrative files handled by rule 3)
    m = _CODING.match(p)
    if m is not None and not m.group("tag").startswith("_"):
        return FileClass("owned", key=m.group("key"), tag=m.group("tag"), stage="coding")

    # 3 - administrative coding file (settlements, notes, ...)
    if _CODING_ADMIN.match(p) is not None:
        return FileClass("admin")

    # 3b - the settlement store: it holds every Master decision on a coder
    #      disagreement, for both screening stages
    if _SETTLEMENTS.match(p) or _SETTLED_LOG.match(p) or _RESOLUTIONS.match(p):
        return FileClass("admin", blind_stages=("abstract", "fulltext"))

    # 4a - the agreed full-text verdicts are recomputed from every coder's file
    #      (audit W8): derived, written by the owner's client only.
    if _FULLTEXT_AGREED.match(p) is not None:
        return FileClass("derived", blind_stages=("fulltext",))
    # 4b — the app's real full-text decision files: "Screening/fulltext - <Tag>.json" (audit W7)
    m = _SCREENING_FT.match(p)
    if m is not None:
        return FileClass("owned", tag=m.group("tag"), stage="fulltext")

    # 4, 5 — whole-stage files, one per coder tag
    m = _SCREENING.match(p)
    if m is not None:
        return FileClass("owned", tag=m.group("tag"), stage="abstract")
    m = _FULLTEXT.match(p)
    if m is not None:
        return FileClass("owned", tag=m.group("tag"), stage="fulltext")

    # 6 — settled extraction
    m = _EXTRACTION.match(p)
    if m is not None:
        return FileClass("shared", key=m.group("key"), task="settle")

    # 7 — reproduction report
    m = _REPRODUCTION.match(p)
    if m is not None:
        return FileClass("shared", key=m.group("key"), task="reproduce")

    # 8 — coder variable assignments
    m = _DATASET_VAR_ASSIGN.match(p)
    if m is not None:
        return FileClass("shared", key=m.group("key"), task="harmonize")

    # 9 — dataset products (prepared/harmonized/variables/dv_direction/...)
    m = _DATASET_PRODUCT.match(p)
    if m is not None:
        return FileClass("shared", key=m.group("key"), task="reproduce")

    # 10 — dataset code (harmonizer, preparation, reproduction scripts)
    m = _DATASET_CODE.match(p)
    if m is not None:
        return FileClass("shared", key=m.group("key"), task="reproduce")

    # 11 — raw data: present in the mirror, never uploaded/changed
    m = _DATASET_ANY.match(p)
    if m is not None:
        return FileClass("immutable", key=m.group("key"))

    # 12 — papers, extracted text, supplements
    if _IMMUTABLE.match(p) is not None:
        return FileClass("immutable", key=_immutable_key(p))

    # 13 - analysis products; pulled separately, never pushed by a coder
    if _MASTER.match(p) is not None:
        # the master workbook carries the AI's and every coder's screening and
        # full-text verdicts
        return FileClass("derived", blind_stages=("abstract", "fulltext"))
    if _PROJECT_RECORDS.match(p) is not None:
        # the decision-free record table: bibliographic columns only, so a
        # blinded coder screens from it (never a verdict in it)
        return FileClass("derived")
    if _PROJECT_BASE.match(p) is not None:
        # the compiler's input: it keeps the curated stages (Effective_Verdict),
        # so it is as sensitive as the master workbook
        return FileClass("admin", blind_stages=("abstract", "fulltext"))
    if _DERIVED.match(p) is not None:
        blind = ("coding",) if _RESULTS_BLIND.search(p[len("Results/"):]) else ()
        return FileClass("derived", blind_stages=blind)

    # 14 — Project/, non-JSON Screening/, everything else
    return FileClass("admin")


def is_ai_route_file(path: str, fc: FileClass | None = None) -> bool:
    """True for the AI coding-route artefacts ``Codings/<Key> - SB|MM|DS.json(.md)``.

    Those are the independent reliability codings (AGENTS.md: the SB / MM / DS
    routes).  They are owned coding files like any other coder's, but they are
    fetched **on demand** (:data:`AI_ROUTE_TAGS`, see :func:`is_core`) because
    they are ~264 MB and no coding workflow reads them.

    Everything else a route owns stays core: a harmonizer or variable-assignment
    draft (``Codings/<Key> - harmonizer|assignments - <Tag>.json``) is a tiny
    override file its own tab walks, and a screening/full-text decision file is
    not a coding file at all.
    """
    p = normalize_path(path)
    if fc is None:
        fc = classify(p)
    return (fc.kind == "owned" and fc.stage == "coding" and fc.tag in AI_ROUTE_TAGS
            and p.lower().endswith((".json", ".md"))
            and _CODING_HARM.match(p) is None)


def is_core(path: str, fc: FileClass | None = None) -> bool:
    """True when *path* must always be mirrored into the client's working copy.

    Core means: anything under :data:`CORE_PREFIXES`, ``master_dataset.xlsx``,
    and every ``owned``/``shared`` file - **except**

    * a dataset CSV product that is not one of the small metadata sidecars in
      :data:`DATASET_CORE_CSVS` (``Datasets/<Key>/<Key> - harmonized.csv``,
      ``- prepared*.csv``, the raw copies ... and their ``.csv.gz`` twins):
      those are fetched on demand (``ensure_local``), because they are 2.2 GB of
      the real project's 2.65 GB core tier;
    * an **AI route coding file** (``Codings/<Key> - SB|MM|DS.json``, see
      :data:`AI_ROUTE_TAGS` / :func:`is_ai_route_file`): ~264 MB of reliability
      codings that only the agreement/IRR views read, so they are fetched on
      demand too.

    Immutable sources (``Papers/``, ``Supplements/``, raw ``Datasets/`` data)
    and ``derived`` ``Results/`` are **not** core - they are fetched on demand /
    pulled separately.  Excluded paths are never core, even when they sit under
    a core prefix (e.g. ``Codings/__pycache__/x.pyc``).
    """
    p = normalize_path(path)
    if fc is None:
        fc = classify(p)

    if fc.kind == "excluded":
        return False
    if p in CORE_ROOT_FILES:
        return True
    if fc.kind == "shared" and p.startswith("Datasets/"):
        return _dataset_product_is_core(p)
    if is_ai_route_file(p, fc):
        return False   # the AI reliability codings are fetched on demand
    if fc.kind in ("owned", "shared"):
        return True
    if fc.kind == "derived" and p.startswith("Results/"):
        return False   # Results snapshots are pulled separately; other derived
                       # files under a core prefix (fulltext_agreed.json) are core
    return any(p.startswith(prefix) for prefix in CORE_PREFIXES)


def _dataset_product_is_core(p: str) -> bool:
    """Whether the shared ``Datasets/<Key>/<Key> - <tail>`` file at *p* is core.

    Only the ``.csv`` products can leave the core tier; every sidecar the app
    reads across all studies without opening one (:data:`DATASET_CORE_CSVS`)
    stays.  Code, notes and JSON products (small) are unaffected.

    A gzip-compressed product (``<Key> - harmonized.csv.gz``) is the same file
    as far as this rule is concerned: it is classified ``shared`` and it leaves
    the core tier exactly like its plain twin.
    """
    m = _DATASET_PRODUCT.match(p)
    if m is None:
        return True
    low = p.lower()
    if not (low.endswith(".csv") or low.endswith(".csv.gz")):
        return True
    tail = p[len("Datasets/" + m.group("key") + "/" + m.group("key") + " - "):]
    if tail.lower().endswith(".gz"):
        tail = tail[:-3]
    return tail in DATASET_CORE_CSVS


# --- hashing --------------------------------------------------------------

_CHUNK = 1024 * 1024


def sha256_bytes(b: bytes) -> str:
    """Hex SHA-256 of *b*."""
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    """Streaming hex SHA-256 of the file at *path* (1 MiB chunks)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
