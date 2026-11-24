"""reset_root_caches keeps every shaped cache usable (regression: KeyError 'rows')."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "coding_web")]

import fulltext_lib  # noqa: E402
import routes_repro  # noqa: E402
import screening_lib  # noqa: E402
import studio_state  # noqa: E402


def test_shaped_caches_keep_their_keys_after_reset():
    studio_state.reset_root_caches()
    assert set(screening_lib._SCREEN_INDEX) == {"stamp", "rows", "by_id", "info"}
    assert screening_lib._SCREEN_INDEX["rows"] is None
    assert set(fulltext_lib._MAP_CACHE) == {"mtime", "map", "info"}
    assert routes_repro._INTERP_CACHE["value"] == (None, "")
