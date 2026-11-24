"""Last forkserver preload: move everything imported so far into the permanent
generation, so the cyclic GC of a forked worker never writes to (and thereby
un-shares) the preloaded pages."""

import gc

gc.collect()
gc.freeze()
