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
