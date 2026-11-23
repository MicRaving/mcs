"""Token storage (INTERFACES §4): the OS keyring, else a user-only file.

* ``keyring`` importable (and a usable backend): service ``CodingStudio-online``,
  account ``<username>@<server>``.
* otherwise ``<mirror>/.studio/token`` (JSON ``{server, username, token}``),
  ``chmod 600`` on POSIX; on Windows inheritance is removed and only the
  current user is granted access via ``icacls`` (best effort).

Tokens are never logged or put in exception messages by this module.
"""

from __future__ import annotations

import json
import os
import subprocess

SERVICE = "CodingStudio-online"

try:                                   # pragma: no cover - depends on the machine
    import keyring as _keyring         # type: ignore
except Exception:                      # ImportError or a broken backend import
    _keyring = None


def _account(server: str, username: str) -> str:
    return f"{username}@{server.rstrip('/')}"


def _token_file(fallback_dir: str) -> str:
    return os.path.join(fallback_dir, "token")


def _restrict(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if not user:
            return
        try:
            subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def save_token(server: str, username: str, token: str, fallback_dir: str | None = None,
               use_keyring: bool = True) -> str:
    """Store *token*; returns ``"keyring"`` or ``"file"`` (or ``"none"``)."""
    if use_keyring and _keyring is not None:
        try:
            _keyring.set_password(SERVICE, _account(server, username), token)
            return "keyring"
        except Exception:
            pass
    if not fallback_dir:
        return "none"
    os.makedirs(fallback_dir, exist_ok=True)
    path = _token_file(fallback_dir)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"server": server, "username": username, "token": token}, fh)
    _restrict(tmp)
    os.replace(tmp, path)
    _restrict(path)
    return "file"


def load_token(server: str, username: str, fallback_dir: str | None = None,
               use_keyring: bool = True) -> str | None:
    if use_keyring and _keyring is not None:
        try:
            tok = _keyring.get_password(SERVICE, _account(server, username))
            if tok:
                return tok
        except Exception:
            pass
    if fallback_dir:
        try:
            with open(_token_file(fallback_dir), encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("server") == server and data.get("username") == username:
                return data.get("token") or None
        except (OSError, ValueError):
            pass
    return None


def delete_token(server: str, username: str, fallback_dir: str | None = None,
                 use_keyring: bool = True) -> None:
    if use_keyring and _keyring is not None:
        try:
            _keyring.delete_password(SERVICE, _account(server, username))
        except Exception:
            pass
    if fallback_dir:
        try:
            os.unlink(_token_file(fallback_dir))
        except OSError:
            pass
