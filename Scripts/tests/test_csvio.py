"""Tests for ``Scripts/csvio.py``: the plain-or-gzip dataset product resolver.

A dataset product may be stored as ``<Key> - harmonized.csv(.gz)``.  The module
exists so that every reader keeps building the same literal path and lets
``csvio`` decide which file is on disk.  These tests pin that contract:

* ``resolve`` prefers the plain file, falls back to the ``.gz`` twin, and returns
  the caller's own path when neither exists (so an error message still names the
  canonical product);
* ``exists``/``glob``/``count_rows``/``read_csv``/``open_text`` are gzip-aware and
  never return a file twice;
* ``compress`` writes a deterministic ``.gz`` and never overwrites one.
"""
import gzip
import os

import pytest

import csvio


def _write(path, text, encoding="utf-8"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding=encoding, newline="") as fh:
        fh.write(text)


def test_resolve_prefers_the_plain_file(tmp_path):
    plain = tmp_path / "K - harmonized.csv"
    _write(str(plain), "a,b\n1,2\n")
    _write(str(plain) + ".gz", "a,b\n1,2\n")
    assert csvio.resolve(str(plain)) == str(plain)
    assert csvio.exists(str(plain))


def test_resolve_falls_back_to_the_gz_twin(tmp_path):
    plain = tmp_path / "K - harmonized.csv"
    with gzip.open(str(plain) + ".gz", "wt", encoding="utf-8", newline="") as fh:
        fh.write("a,b\n1,2\n")
    assert csvio.resolve(str(plain)) == str(plain) + ".gz"
    assert csvio.exists(str(plain))
    assert not os.path.isfile(str(plain))


def test_resolve_returns_the_caller_path_when_neither_exists(tmp_path):
    missing = str(tmp_path / "K - harmonized.csv")
    assert csvio.resolve(missing) == missing
    assert not csvio.exists(missing)
    assert not csvio.exists("")
    # a path that is already compressed is never given a second suffix
    assert csvio.resolve(missing + ".gz") == missing + ".gz"


def test_glob_matches_both_spellings_once(tmp_path):
    _write(str(tmp_path / "A" / "A - harmonized.csv"), "x\n1\n")
    os.makedirs(str(tmp_path / "B"))
    with gzip.open(str(tmp_path / "B" / "B - harmonized.csv.gz"), "wt",
                   encoding="utf-8", newline="") as fh:
        fh.write("x\n1\n")
    found = csvio.glob(str(tmp_path / "*" / "* - harmonized.csv"))
    assert [os.path.basename(p) for p in found] == ["A - harmonized.csv",
                                                    "B - harmonized.csv.gz"]


def test_glob_prefers_plain_over_its_own_twin(tmp_path):
    _write(str(tmp_path / "K" / "K - harmonized.csv"), "x\n1\n")
    with gzip.open(str(tmp_path / "K" / "K - harmonized.csv.gz"), "wt",
                   encoding="utf-8", newline="") as fh:
        fh.write("x\n1\n")
    found = csvio.glob(str(tmp_path / "*" / "* - harmonized.csv"))
    assert len(found) == 1
    assert found[0].endswith("K - harmonized.csv")


def test_read_csv_count_rows_and_header_on_both_twins(tmp_path):
    p = tmp_path / "K - harmonized.csv"
    _write(str(p), "a,b,c\n1,2,3\n4,5,6\n")
    plain = csvio.read_csv(str(p))
    assert list(plain.columns) == ["a", "b", "c"]
    assert len(plain) == 2
    assert csvio.read_header(str(p)) == ["a", "b", "c"]
    assert csvio.count_rows(str(p)) == 2

    with gzip.open(str(p) + ".gz", "wt", encoding="utf-8", newline="") as fh:
        fh.write("a,b,c\n1,2,3\n4,5,6\n")
    os.remove(str(p))
    assert list(csvio.read_csv(str(p)).columns) == ["a", "b", "c"]
    assert csvio.count_rows(str(p)) == 2
    with csvio.open_text(str(p)) as fh:
        assert fh.readline().startswith("a,b,c")
    with csvio.open_binary(str(p)) as fh:
        assert fh.readline().startswith(b"a,b,c")


def test_compress_is_deterministic_and_never_overwrites(tmp_path):
    p = tmp_path / "K - harmonized.csv"
    _write(str(p), "a\n1\n2\n")
    dst, n_plain, n_gz = csvio.compress(str(p))
    assert dst == str(p) + ".gz"
    assert n_plain == os.path.getsize(str(p))
    assert n_gz == os.path.getsize(dst)
    assert csvio.count_rows(dst) == 2
    first = open(dst, "rb").read()

    _write(str(p), "a\n9\n9\n9\n")          # the plain file changed
    dst2, _n1, _n2 = csvio.compress(str(p))
    assert dst2 == dst
    assert open(dst2, "rb").read() == first  # an existing .gz is never replaced


def test_compress_refuses_a_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        csvio.compress(str(tmp_path / "nope.csv"))


def test_gzip_twin_is_never_treated_as_raw_data():
    """A compressed product keeps its product identity for the protocol."""
    from online_shared import protocol

    for name in ("harmonized.csv.gz", "prepared.csv.gz", "harmonized - SB.csv.gz"):
        path = f"Datasets/K/K - {name}"
        fc = protocol.classify(path)
        assert fc.kind == "shared", (path, fc)
        assert fc.key == "K"
        assert protocol.is_core(path) is False   # a shared product stays out of core


def test_atomic_csv_keeps_the_compressed_destination_compressed(tmp_path):
    """Writing through a ``.csv.gz`` path must produce real gzip, not plain text.

    ``variable_assignments._atomic_csv`` writes a temp file and renames it onto
    the target; the temp must carry the same compression suffix or the product
    becomes an unreadable ``.gz``.
    """
    import pandas as pd
    import variable_assignments as VA

    target = tmp_path / "K - harmonized.csv.gz"
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    old = VA.TMP_DIR
    try:
        VA.TMP_DIR = str(tmp_path)
        VA._atomic_csv(df, str(target))
    finally:
        VA.TMP_DIR = old
    assert target.read_bytes()[:2] == b"\x1f\x8b"      # gzip magic
    back = csvio.read_csv(str(tmp_path / "K - harmonized.csv"))
    assert back.equals(df)
    assert not [p for p in os.listdir(str(tmp_path)) if p.startswith(".__tmp_")]
