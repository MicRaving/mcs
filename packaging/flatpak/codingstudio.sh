#!/bin/sh
# CodingStudio inside the Flatpak sandbox.
#
# The app is a local HTTP server plus a window.  The bundled build carries its
# own Python and its own copies of Flask/PyMuPDF, but not a WebKit runtime, so
# inside the sandbox it runs headless and hands the URL to the user's browser
# through the desktop portal.  The Flatpak shares the host network, so the
# server on 127.0.0.1 is reachable from that browser.
#
# A project folder is opened from the Home view; the sandbox allows the whole
# home directory, so any project kept there can be selected.

# be somewhere sensible when no project has been opened yet
cd "$HOME" 2>/dev/null || cd /

exec /app/codingstudio/CodingStudioUni --browser "$@"
