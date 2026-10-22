# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the portable CodingStudio desktop app.

Trimmed deliberately:
  * PyMuPDF's ``mupdf-devel`` headers/import libs (~6.5 MB) are dropped — they
    are build-time files only.
  * pypdf is not bundled (page counts use PyMuPDF); this also removes the
    transitive ``cryptography`` payload.

Numeric stack (D6): ``numpy`` / ``scipy`` are bundled so the app's statistics run
in-process through the shared ``Scripts/stat_core.py`` (no silent degradation to
a second implementation).  ``pandas`` is bundled too because the shared spec
selector ``Scripts/specs_api.py`` (and ``suite_common``) require it — the app's
analysis path imports it directly.

Export support is bundled: ``openpyxl`` (Excel) and ``pptx`` (PowerPoint).  The
latter pulls in ``lxml`` and ``Pillow``, which are therefore kept.

Version stamp: ``Scripts/coding_web/studio_lib.APP_VERSION`` (also printed by
``python Scripts/build_coding_exe.py --version`` and served at ``/api/root``).

``CODINGSTUDIO_LITE=1`` builds the ~13 MB variant without PyMuPDF: the PDF pane
still renders (the WebView2 viewer draws it) but live text extraction and page
counts fall back to ``Text/<Key>.txt``.

``CODINGSTUDIO_NAME`` sets the distribution's name (the executable, the
``_internal/`` folder and the folder at the repo root).  It is how a branch
builds beside an installed app instead of over it: ``--name CodingStudioUni``.

Build:  python Scripts/build_coding_exe.py [--lite] [--onefile] [--name NAME]
        (default: onedir)
"""

import os

from PyInstaller.utils.hooks import collect_all

APP = "app.py"
LITE = os.environ.get("CODINGSTUDIO_LITE") == "1"
# The shared pipeline modules live one level up (Scripts/), not in coding_web/.
SCRIPTS_DIR = os.path.normpath(os.path.join(SPECPATH, ".."))

datas, binaries, hiddenimports = [], [], []

# Export formats (always) + PDF text extraction (non-lite) + the native window
# + the numeric stack the shared estimators need (D6).
packages = ("openpyxl", "pptx", "docx", "lxml", "PIL", "webview", "pythonnet",
            "clr_loader", "numpy", "scipy", "pandas")
if not LITE:
    packages = ("pymupdf",) + packages
for pkg in packages:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# drop PyMuPDF's development bundle (headers + .lib), never used at runtime
datas = [item for item in datas if "mupdf-devel" not in item[0].lower().replace("\\", "/")]
datas = [item for item in datas if "__pycache__" not in item[0].lower()]

# Drop Pillow's AVIF codec: the app never reads AVIF (exports are PNG/XLSX/PPTX),
# and `_avif.cp311-win_amd64.pyd` is a large binary that triggered a bootloader
# "decompression resulted in return code -3" during onefile self-extraction.
binaries = [b for b in binaries if "avif" not in (b[0] or "").lower()]
datas = [d for d in datas if "avif" not in (d[0] or "").lower()]

datas += [("static", "static"), ("schema.json", ".")]
# the schemes travel with the app: Schemes/<id>/{scheme,criteria}.json
datas += [("Schemes", "Schemes")]
hiddenimports += [
    "flask", "clr", "webview.platforms.winforms", "webview.platforms.edgechromium",
    "openpyxl", "et_xmlfile", "pptx", "lxml", "lxml.etree", "PIL", "PIL.Image",
    "docx", "typing_extensions",
    # shared pipeline modules imported in-process by the app (D6 / interface §2-§4)
    "contract", "stat_core", "specs_api", "prisma", "prisma_report",
    "domain_restriction", "study_exclusions", "suite_common",
    # the project layer: the active project, its scheme and its criteria
    "project_context", "scheme_lib", "scheme_legacy", "criteria_lib",
    # the agent bundle (the JSON Schema, the prompt and the MCP config)
    "scheme_export",
]

excludes = [
    "tkinter", "matplotlib",
    "cryptography", "pypdf", "pdfminer", "pymupdf4llm", "IPython", "pytest",
    "setuptools", "pkg_resources", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "PIL.AvifImagePlugin",
    # Deep-learning stacks are not used by the app; their PyInstaller hooks can
    # abort the build in this environment (broken/heavy torch install).
    "torch", "torchvision", "torchaudio", "tensorflow", "jax", "jaxlib",
    "keras", "onnx", "onnxruntime", "statsmodels", "sklearn", "numba",
]
if LITE:
    excludes += ["pymupdf", "fitz", "pymupdf.utils", "pymupdf.table"]

a = Analysis(
    [APP],
    # local modules: studio_state / studio_lib / routes_* / statslib / analysis_lib
    # + the shared Scripts/ modules (contract, stat_core, specs_api, prisma, ...)
    pathex=[SPECPATH, SCRIPTS_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

NAME = os.environ.get("CODINGSTUDIO_NAME") or (
    "CodingStudioLite" if LITE else "CodingStudio")
# Default = ONEDIR: no runtime self-extraction, so an antivirus scanner or a
# cloud-synced folder can never corrupt the bootloader unpack (the onefile mode
# failed with "decompression resulted in return code -3" on the AVIF binary).
# Set CODINGSTUDIO_ONEFILE=1 for the legacy single-file build.
ONEFILE = os.environ.get("CODINGSTUDIO_ONEFILE") == "1"

_common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # self-contained GUI: no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        **_common,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **_common,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
