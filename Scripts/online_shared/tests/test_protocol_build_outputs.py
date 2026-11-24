"""Build outputs and tool caches are never synced or published."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from online_shared import protocol as P  # noqa: E402


def test_build_outputs_and_caches_are_excluded():
    for p in ("Scripts/coding_web/dist/CodingStudio/_internal/python311.dll",
              "Scripts/coding_web/dist/",
              "Scripts/coding_web/build/x/y.toc",
              ".ruff_cache/0.1/abc", "Scripts/.pytest_cache/v/x",
              ".openchamber/state.json",
              "metacognition/metacog_studies.xlsx", "metacognition/Scripts/metacognition.py"):
        assert P.classify(p).kind == "excluded", p


def test_scripts_themselves_still_sync():
    for p in ("Scripts/coding_web/app.py", "Scripts/compile_master.py",
              "Scripts/coding_web/static/app.js"):
        assert P.classify(p).kind != "excluded", p
