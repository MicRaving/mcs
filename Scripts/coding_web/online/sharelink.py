"""Share-link projects on a Nextcloud: the **owner** side (PLAN §13, D11).

A Nextcloud *public share link* with a password and edit rights reaches exactly
one folder.  That is what makes it a good boundary for co-authors who have no
Nextcloud account: the owner shares a folder (not his account, not his app
password) and the coder joins with an invitation
(:mod:`~.invite`).

This module holds the three server-facing pieces:

* :class:`OcsClient` - `OCS <https://docs.nextcloud.com/server/stable/developer_manual/client_apis/OCS/ocs-api-overview.html>`_
  share create / update-password / delete (``POST|PUT|DELETE
  /ocs/v2.php/apps/files_sharing/api/v1/shares``), plus a capability probe so
  the UI can hide the feature on a server that has no OCS (or is not a
  Nextcloud at all: LRZ Sync+Share).
* :func:`parse_share_url` - accepts the URL Nextcloud shows in its UI
  (``https://host/s/<token>``, with or without ``index.php``) and the public
  WebDAV form the app actually talks to
  (``https://host/public.php/dav/files/<token>``).
* the link document in ``.studio/link.json`` (``share_id``, ``token``,
  ``public_root``, ``created_at`` - **never** the password) and the link
  password in the owner's keyring, keyed by the public root, so invitations can
  be issued later without asking again.

Nothing here logs or returns the password; :class:`OcsClient` never holds it
either (only the owner's app password in the WebDAV instance, which its
``__repr__`` does not expose).
"""

from __future__ import annotations

import json
import secrets
import string
import urllib.parse

from . import keyring_store
from .errors import BadRequestError, ForbiddenError, NotFoundError, OnlineError
from .nextcloud import origin_of
from .webdav import WebDAV, is_loopback

#: The public WebDAV root a link exposes (``<host>/public.php/dav/files/<token>``).
SHARE_FILE = ".studio/link.json"

#: The keyring account the link password is stored under, keyed by the public
#: root as the "server" (``share-link@https://host/public.php/dav/files/<tok>``).
LINK_ACCOUNT = "share-link"

#: Share type 3 = public link, permissions 15 = read + update + create + delete.
SHARE_TYPE_LINK = 3
PERMISSIONS_EDIT = 15

_OCS_SHARES = "ocs/v2.php/apps/files_sharing/api/v1/shares"
_OCS_CAPABILITIES = "ocs/v2.php/cloud/capabilities"

#: characters for a link password: a policy-safe mix (digits, upper, lower,
#: symbol) that survives being read aloud and typed on a phone
_PW_LOWER = string.ascii_lowercase
_PW_UPPER = string.ascii_uppercase
_PW_DIGIT = string.digits
_PW_SYMBOL = "!@#$%^&*-_=+"


class ShareLinkError(ValueError):
    """A refused share-link operation; ``code`` is stable, the message is human."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class ShareLinkUnavailable(ShareLinkError):
    """This server has no OCS sharing API (plain WebDAV, LRZ Sync+Share, 404/403)."""


# ---------------------------------------------------------------------------
# passwords, URLs
# ---------------------------------------------------------------------------

def random_password(length: int = 24) -> str:
    """A random link password with at least one lower, upper, digit and symbol."""
    length = max(12, int(length or 24))
    pools = (_PW_LOWER, _PW_UPPER, _PW_DIGIT, _PW_SYMBOL)
    chars = [secrets.choice(pool) for pool in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def public_root_for(host_or_url: str, token: str) -> str:
    """``https://host`` + share *token* -> the WebDAV root of the link."""
    origin = origin_of(host_or_url).rstrip("/")
    return f"{origin}/public.php/dav/files/{urllib.parse.quote(str(token), safe='')}"


def share_url_for(public_root: str) -> str:
    """The human URL of a public root (``https://host/s/<token>``)."""
    root = str(public_root or "").rstrip("/")
    token = root.rsplit("/", 1)[-1]
    return f"{origin_of(root)}/s/{token}" if token else root


def is_public_root(url: str) -> bool:
    return "/public.php/" in str(url or "")


def parse_share_url(text: str, base_url: str | None = None) -> dict:
    """A share link the owner pasted -> ``{token, public_root, url}``.

    Accepts what Nextcloud's UI copies (``https://host/s/<token>``,
    ``…/index.php/s/<token>``, ``…/s/<token>/download``) and the public WebDAV
    form the app speaks.  A bare token (or ``/s/<token>``) is resolved against
    *base_url* (the Nextcloud the owner is connected to).
    """
    raw = str(text or "").strip()
    if not raw:
        raise ShareLinkError("bad_link", "Paste the share link Nextcloud showed you "
                                        "(https://…/s/<token>) or choose “Create a share link”.")
    if "://" not in raw:
        base = str(base_url or "").strip()
        if not base:
            raise ShareLinkError("bad_link", "That is not a full link. Paste the whole "
                                            "https://…/s/<token> link.")
        raw = origin_of(base).rstrip("/") + "/" + raw.lstrip("/")
    split = urllib.parse.urlsplit(raw)
    path = split.path or ""
    token = ""
    marker = "/public.php/dav/files/"
    if marker in path:
        token = path.split(marker, 1)[1].split("/", 1)[0]
    else:
        parts = [p for p in path.split("/") if p]
        if "s" in parts:
            idx = parts.index("s")
            if len(parts) > idx + 1:
                token = parts[idx + 1]
    token = urllib.parse.unquote(token).strip()
    if not token:
        raise ShareLinkError("bad_link", "That does not look like a Nextcloud share link: "
                                        "the address must contain “/s/<token>”.")
    return {"token": token, "public_root": public_root_for(raw, token),
            "url": f"{origin_of(raw)}/s/{token}"}


# ---------------------------------------------------------------------------
# OCS: share create / password / delete
# ---------------------------------------------------------------------------

class OcsClient:
    """The owner's Nextcloud, addressed at its OCS sharing API."""

    def __init__(self, base_url: str, username: str, app_password: str, *,
                 timeout: float = 30.0, allow_insecure_http: bool | None = None,
                 dav: WebDAV | None = None):
        self.base_url = origin_of(base_url)
        if allow_insecure_http is None:
            allow_insecure_http = is_loopback(base_url)
        self.dav = dav or WebDAV(self.base_url, username, app_password, timeout=timeout,
                                 allow_insecure_http=bool(allow_insecure_http))

    def __repr__(self) -> str:                    # never leak the app password
        return f"OcsClient({self.base_url!r})"

    def close(self) -> None:
        try:
            self.dav.close()
        except Exception:                          # noqa: BLE001
            pass

    # -- one OCS call -------------------------------------------------------------

    def _call(self, method: str, path: str, form: dict | None = None) -> object:
        body = urllib.parse.urlencode(form or {}, doseq=True).encode("utf-8") if form is not None else None
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            resp = self.dav.request(method, path, headers=headers, body=body,
                                    query={"format": "json"})
        except (ForbiddenError, NotFoundError, BadRequestError, OnlineError) as exc:
            raise ShareLinkUnavailable(
                "ocs_unavailable",
                "This server has no Nextcloud sharing API, so a share link cannot be "
                "created from here. Create the link in Nextcloud (Files → Share) and "
                "paste it below.", getattr(exc, "status", None)) from exc
        try:
            data = json.loads((resp.body or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ShareLinkUnavailable(
                "ocs_unavailable",
                "This server answered like a plain WebDAV share, not like a Nextcloud; "
                "create the link in Nextcloud and paste it below.") from None
        ocs = data.get("ocs") if isinstance(data, dict) else None
        if not isinstance(ocs, dict):
            raise ShareLinkUnavailable("ocs_unavailable",
                                       "This server has no Nextcloud sharing API.")
        meta = ocs.get("meta") if isinstance(ocs.get("meta"), dict) else {}
        try:
            code = int(meta.get("statuscode") or 0)
        except (TypeError, ValueError):
            code = 0
        if code not in (100, 200):
            raise ShareLinkError(f"ocs_{code or 'error'}",
                                 str(meta.get("message") or "Nextcloud refused the request"),
                                 code)
        return ocs.get("data")

    # -- the calls the UI needs ------------------------------------------------------

    def capabilities(self) -> dict:
        data = self._call("GET", _OCS_CAPABILITIES)
        return data if isinstance(data, dict) else {}

    def available(self) -> bool:
        try:
            self.capabilities()
            return True
        except ShareLinkError:
            return False

    def create_share(self, path: str, password: str) -> dict:
        """Create the public link for *path* (relative to the owner's files)."""
        folder = "/" + str(path or "").strip("/") if str(path or "").strip("/") else "/"
        data = self._call("POST", _OCS_SHARES, {
            "path": folder,
            "shareType": SHARE_TYPE_LINK,
            "permissions": PERMISSIONS_EDIT,
            "password": password,
        })
        if not isinstance(data, dict):
            raise ShareLinkError("ocs_error", "Nextcloud did not return the new share")
        token = str(data.get("token") or "").strip()
        share_id = str(data.get("id") or "").strip()
        if not token or not share_id:
            raise ShareLinkError("ocs_error", "Nextcloud did not return a share token")
        return {"share_id": share_id, "token": token,
                "url": str(data.get("url") or "").strip() or share_url_for(public_root_for(self.base_url, token))}

    def update_password(self, share_id: str, password: str) -> None:
        self._call("PUT", f"{_OCS_SHARES}/{urllib.parse.quote(str(share_id), safe='')}",
                   {"password": password})

    def delete_share(self, share_id: str) -> None:
        try:
            self._call("DELETE", f"{_OCS_SHARES}/{urllib.parse.quote(str(share_id), safe='')}")
        except ShareLinkError as exc:
            if exc.code == "ocs_404":              # already gone: that is the wanted state
                return
            raise

    def list_shares(self, path: str | None = None) -> list[dict]:
        path = (("/" + path.strip("/")) if path and path.strip("/") else "")
        data = self._call("GET", _OCS_SHARES, {"path": path} if path else None)
        return [dict(row) for row in (data or []) if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# the link password (the owner's keyring, keyed by the public root)
# ---------------------------------------------------------------------------

def link_password(public_root: str, fallback_dir: str | None = None,
                  use_keyring: bool = True) -> str | None:
    return keyring_store.load_token(public_root, LINK_ACCOUNT, fallback_dir,
                                    use_keyring=use_keyring)


def save_link_password(public_root: str, password: str, fallback_dir: str | None = None,
                       use_keyring: bool = True) -> str:
    return keyring_store.save_token(public_root, LINK_ACCOUNT, password,
                                    fallback_dir=fallback_dir, use_keyring=use_keyring)


def forget_link_password(public_root: str, fallback_dir: str | None = None,
                         use_keyring: bool = True) -> None:
    keyring_store.delete_token(public_root, LINK_ACCOUNT, fallback_dir, use_keyring=use_keyring)


def link_keyring_dir(mirror) -> str | None:
    """Where the file fallback of the link password may live (never ``.studio/token``)."""
    studio = getattr(mirror, "studio", None)
    return f"{studio}/link" if studio else None
