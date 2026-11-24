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

The Windows and macOS jobs install `requirements.txt` (including the native
window backend: pythonnet on Windows, PyObjC on macOS, and the build fails if
it cannot be imported), run `Scripts/build_coding_exe.py` on a native runner
(PyInstaller cannot cross-compile) and upload the result.  The Flatpak runs the
app from source against the GNOME runtime, whose WebKitGTK gives it a native
window too.

The version has one source of truth, `APP_VERSION` in
`Scripts/coding_web/studio_lib.py`: the installer is named and stamped from it,
and a `v*` tag that disagrees with it fails the build.

The app opens in its own window.  When the window cannot be created it falls
back to the default browser and writes the reason (the backend it tried and the
full traceback) to `<project>/temp/CodingStudio.log`.  On Windows the app
removes the "downloaded from the internet" mark from its own .NET assemblies
on start - with the mark, a zip extracted by Explorer could not load pythonnet
and always ended up in the browser (fixed after 0.2.1; for 0.2.1 itself, run
`Unblock-File` on the zip before extracting it, or use the installer).  On
macOS, a quarantined app that is started from the Downloads folder runs from a
read-only copy; move it to Applications first so it can update itself.

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
*Restore template* to start again from a fresh one for this project; the old
copy is kept in `archive/`).  A save is refused when the file was changed on
disk after the editor opened it.

The same view holds **Updates**: the app asks GitHub for the latest release of
this repository, downloads the build for the current platform (sha256-checked
against the release) and installs it on restart.  A portable copy (the zip) is
replaced folder-for-folder; a copy installed with the Windows installer runs the
next installer instead (`/SILENT`, which may ask for administrator rights);
macOS uses the `.zip`, not the `.dmg`.  A Flatpak is updated by the system
(`flatpak update`), and a source run only links to the release page - neither
can replace itself.