"""Token storage (INTERFACES §4): the OS keyring, else a user-only file.

* ``keyring`` importable (and a usable backend): service ``CodingStudio-online``,
  account ``<username>@<server>``.
* otherwise ``<mirror>/.studio/token`` (JSON ``{server, username, token}``),
  ``chmod 600`` on POSIX; on Windows inheritance is removed and only the
  current user is granted access via ``icacls`` (best effort).
* ``CODINGSTUDIO_NO_KEYRING=1`` disables the keyring path entirely (token file
  only), for a portable checkout or an automated run.

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

class _WinCred:
    """The Windows Credential Manager through ``advapi32`` (no dependency).

    The packaged app does not ship the ``keyring`` package, so before
    2026-11-25 every token / app password ended up in the mirror's fallback
    file.  This is the same store ``keyring``'s Windows backend writes to: a
    generic credential ``<service>:<account>``, persisted for this user on
    this machine, readable only by this user's logon session.
    """

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                        ("UserName", wintypes.LPWSTR)]

        self._ct = ctypes
        self._CRED = CREDENTIAL
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self._write = adv.CredWriteW
        self._write.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._read = adv.CredReadW
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self._read.restype = wintypes.BOOL
        self._delete = adv.CredDeleteW
        self._delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._delete.restype = wintypes.BOOL
        self._free = adv.CredFree
        self._free.argtypes = [ctypes.c_void_p]

    @staticmethod
    def _target(service: str, account: str) -> str:
        return f"{service}:{account}"

    def set_password(self, service: str, account: str, secret: str) -> None:
        ct = self._ct
        data = secret.encode("utf-8")
        buf = (ct.c_ubyte * len(data)).from_buffer_copy(data) if data else (ct.c_ubyte * 1)()
        cred = self._CRED()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = self._target(service, account)
        cred.CredentialBlobSize = len(data)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = account
        if not self._write(ct.byref(cred), 0):
            raise OSError(ct.get_last_error(), "CredWriteW failed")

    def get_password(self, service: str, account: str) -> str | None:
        ct = self._ct
        ptr = ct.POINTER(self._CRED)()
        if not self._read(self._target(service, account), self.CRED_TYPE_GENERIC, 0, ct.byref(ptr)):
            err = ct.get_last_error()
            if err == self.ERROR_NOT_FOUND:
                return None
            raise OSError(err, "CredReadW failed")
        try:
            cred = ptr.contents
            n = int(cred.CredentialBlobSize)
            raw = ct.string_at(cred.CredentialBlob, n) if n else b""
            return raw.decode("utf-8")
        finally:
            self._free(ptr)

    def delete_password(self, service: str, account: str) -> None:
        ct = self._ct
        if not self._delete(self._target(service, account), self.CRED_TYPE_GENERIC, 0):
            err = ct.get_last_error()
            if err != self.ERROR_NOT_FOUND:
                raise OSError(err, "CredDeleteW failed")


if _keyring is None and os.name == "nt":       # pragma: no cover - Windows only
    try:
        _keyring = _WinCred()
    except Exception:                          # no advapi32 (Wine, a stripped system)
        _keyring = None

#: ``CODINGSTUDIO_NO_KEYRING=1`` keeps the token out of the OS keyring (a
#: portable checkout, a shared machine, an automated end-to-end run).
if (os.environ.get("CODINGSTUDIO_NO_KEYRING") or "").strip().lower() in ("1", "true", "yes"):
    _keyring = None


def backend_name() -> str:
    """``"wincred"`` / ``"keyring"`` / ``"file"`` - where a secret goes."""
    if _keyring is None:
        return "file"
    return "wincred" if isinstance(_keyring, _WinCred) else "keyring"


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
            if fallback_dir:
                # a file written by an older build (no keyring then): the
                # secret now lives in the keyring, so the plain copy goes
                _drop_file(fallback_dir, server, username)
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
                tok = data.get("token") or None
                if tok and use_keyring and _keyring is not None:
                    # migrate a fallback file into the keyring (and drop it)
                    try:
                        _keyring.set_password(SERVICE, _account(server, username), tok)
                        _drop_file(fallback_dir, server, username)
                    except Exception:
                        pass
                return tok
        except (OSError, ValueError):
            pass
    return None


def _drop_file(fallback_dir: str, server: str, username: str) -> None:
    """Remove the fallback file when it holds this account's secret."""
    path = _token_file(fallback_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if data.get("server") == server and data.get("username") == username:
        try:
            os.unlink(path)
        except OSError:
            pass


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
