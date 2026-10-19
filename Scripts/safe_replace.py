# -*- coding: utf-8 -*-
"""Atomic file replace with a short retry for Windows/Nextcloud file locks.

Windows (and the Nextcloud sync client, Excel, antivirus) can briefly hold the
destination of an ``os.replace`` open, raising ``PermissionError``; a plain
``os.replace`` then crashes a long pipeline run.  ``safe_replace`` retries with a
linear back-off and only re-raises if every attempt fails, so a genuine
permission problem still surfaces while a transient lock does not.
"""
import os
import time


def safe_replace(src, dst, attempts=6, base_delay=0.5):
    """``os.replace(src, dst)`` with a bounded retry on ``PermissionError``."""
    last = None
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return True
        except PermissionError as e:
            last = e
            if i < attempts - 1:
                time.sleep(base_delay * (i + 1))
    if last is not None:
        raise last
    return False
