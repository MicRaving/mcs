"""The archive Accept header must name exactly one type (Nextcloud matches it verbatim)."""
from online import ncarchive


def test_accept_is_a_single_type():
    assert ncarchive.ACCEPT in (ncarchive.TAR, ncarchive.ZIP)
    assert "," not in ncarchive.ACCEPT
