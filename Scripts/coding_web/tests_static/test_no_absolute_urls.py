"""Lint: the CodingStudio frontend must not hard-code absolute app URLs.

The page is served at "/" on the desktop and at "/app/p/<pid>/" in the browser,
so every app URL has to be relative ("api/...", "static/...", "paper/...").
A string or template literal that starts with an absolute app path fails the
build.  A line that genuinely needs one may carry the escape comment
`// abs-url-ok` (or `<!-- abs-url-ok -->` in HTML).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"

# a quote (or backtick) directly followed by an absolute app path
FORBIDDEN = re.compile(r"""(["'`])/(api|static|paper|pageimg|supplement)(?=[/"'`?])""")
ESCAPE = "abs-url-ok"

TARGETS = sorted(
    [p for p in STATIC.glob("*.js")]
    + [p for p in STATIC.glob("*.css")]
    + [p for p in STATIC.glob("*.html")]
)


def test_targets_exist():
    names = {p.name for p in TARGETS}
    assert "index.html" in names
    assert "app.js" in names
    assert "browser.js" in names


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_no_absolute_app_urls(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    bad = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if ESCAPE in line:
            continue
        for m in FORBIDDEN.finditer(line):
            bad.append(f"{path.name}:{lineno}: {line.strip()[:160]}")
    assert not bad, "absolute app URL(s) - use a relative form or add // abs-url-ok:\n" + "\n".join(bad)
