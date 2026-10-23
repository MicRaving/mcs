#!/bin/sh
# CodingStudio inside the Flatpak sandbox.
#
# The app runs against the GNOME runtime's python3, which ships PyGObject and
# WebKitGTK: pywebview opens a native window rather than a browser tab.  The
# pip dependencies live in /app/lib/python3.x/site-packages.
#
# The config file and the project folders are both writable: the former in the
# sandboxed XDG config dir, the latter under the home directory (the Flatpak
# allows the whole home).

PY=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3.11)
export PYTHONPATH="/app/lib/python$PY/site-packages${PYTHONPATH:+:$PYTHONPATH}"
cd "$HOME" 2>/dev/null || cd /

exec python3 /app/codingstudio/app.py "$@"
