# MetaCodingStudio (mcs)

A portable desktop/web application for coding misinformation-intervention
experiments into a fixed schema, managing agreement between coders, and running
the analysis / re-analysis pipeline over the coded corpus.

## Layout

| Path | What it is |
|---|---|
| `Scripts/coding_web/` | The application: Flask backend, static SPA, PyInstaller spec |
| `Scripts/coding_web/schema.json` | The coding schema (v3) used by the coder UI and the validator |
| `Scripts/*.py` | Shared analysis code the app imports (statistics, spec tables, contract, validation) |
| `Scripts/analysis/` | The analysis pipeline the app can run (meta-analysis, SDT re-analysis, harmonizers) |
| `run_analysis.py` | Entry point for the analysis pipeline |

The application is data-agnostic: a *project folder* holds the research data
(`Papers/`, `Text/`, `Extraction/`, `Codings/`, `Datasets/`, `Results/`), which is
never committed here.

## Run (from source)

```
pip install -r requirements.txt
python Scripts/coding_web/app.py --root <project folder>
```

## Build the desktop app

```
python Scripts/build_coding_exe.py          # onedir (default) -> CodingStudio/
python Scripts/build_coding_exe.py --onefile
python Scripts/build_coding_exe.py --lite   # without PyMuPDF
```

## Builds for Windows, macOS and Linux

`.github/workflows/build.yml` builds all three on every push and publishes them
as a release on a `v*` tag:

| Artifact | What it is |
|---|---|
| `CodingStudioUni-windows-x64.zip` | onedir build: unzip, run `CodingStudioUni.exe` |
| `CodingStudioUni-<ver>-windows-x64-setup.exe` | Inno Setup installer (Windows) |
| `CodingStudioUni-macos-arm64.zip` | `.app` bundle (Apple silicon) |
| `CodingStudioUni-macos-arm64.dmg` | drag-to-install disk image |
| `CodingStudioUni-macos-x86_64.zip` | `.app` bundle (Intel) |
| `CodingStudioUni-macos-x86_64.dmg` | drag-to-install disk image |
| `CodingStudioUni-linux-x64.flatpak` | `flatpak install --user <file>` |

Each job installs `requirements.txt`, runs `Scripts/build_coding_exe.py` on a
native runner (PyInstaller cannot cross-compile) and uploads the result.  The
Flatpak wraps that same self-contained bundle: the sandbox only receives it,
and because the bundle carries no WebKit runtime the app runs headless there
and opens its URL in the user's browser.

Build locally with the same command the workflow uses:

```
python Scripts/build_coding_exe.py --name CodingStudioUni
```

## Checking the layout

`Scripts/coding_web/static/dev/overflow-audit.html` opens the app in an iframe
at four window sizes, walks every tab and reports anything that overflows its
box or leaves the viewport.  Open it from a running app:

```
python Scripts/coding_web/app.py --root <project folder>
# then browse to /static/dev/overflow-audit.html
```

## Settings

Every project carries an `AGENTS.md` beside its folders: what the project is,
which scheme it codes against, and the rules that keep the corpus valid.  It is
written when the project is created, and the **Settings** view edits it (with
*Restore template* to start again from a fresh one for this project).

The same view holds **Updates**: the app asks GitHub for the latest release of
this repository, offers the build for the current platform and installs it on
restart.  A Flatpak is updated by the system (`flatpak update`), and a source
run only links to the release page - neither can replace itself.