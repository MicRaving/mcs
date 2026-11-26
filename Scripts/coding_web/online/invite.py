"""Invitation tokens for share-link Nextcloud projects (PLAN §13, D11).

Why an invitation at all: a Nextcloud **public share link** (``/s/<token>``,
password protected, edit rights) reaches one folder and nothing else, so a
co-author no longer needs a Nextcloud account - and the owner never hands out
his own app password.  What the coder still needs is the link's password *and*
an app identity (a username, a display name, tags), which the owner sets up in
``.studio/members.json``.  One text carries both:

    csinvite1.<base64url(JSON)>

The JSON is::

    {"v": 1, "kind": "nc-link", "root": "https://host/public.php/dav/files/<token>",
     "project": "<folder name>",
     "member": {"username": "anna", "display_name": "Anna", "invite_id": "…",
                "tags": ["AN"]},
     "secret": {"plain": "<link password>"},
     "issued_at": "2026-11-26T12:00:00Z"}

or, when the owner ticks "protect with a passphrase"::

     "secret": {"scrypt": {"n": 32768, "r": 8, "p": 1, "salt": "…"},
                "nonce": "…", "ct": "…", "mac": "…"}

The passphrase protection uses **only the stdlib** (the app bundle does not
ship ``cryptography``): ``hashlib.scrypt`` derives 64 bytes, split into an
encryption key and a MAC key; the password is XORed with HMAC-SHA256
keystream blocks and the whole header is authenticated, so a modified
invitation is refused *before* anything is decrypted.

The owner's problem is "unknowing or disorganized", not hostile: the plain
form is the default because friction is what makes people share their own app
password instead.  The link itself is folder-scoped and revocable.

Parsing is forgiving: mail clients wrap long lines and add prose, so the token
is found by regex and all whitespace inside it is ignored.  Errors are meant
to be shown to a person ("this is not an invitation", "wrong passphrase",
"this invitation was made by a newer version of the app").
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

#: The token prefix.  The trailing dot is part of the marker, so the version is
#: readable in a log or an e-mail without decoding anything.
PREFIX = "csinvite1."
KIND = "nc-link"
VERSION = 1

#: scrypt cost: ~0.1 s and 32 MB on the owner's laptop - enough for a
#: share-link password that is only ever guessed by hand.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
DERIVED = 64                     # 32 bytes encryption key + 32 bytes MAC key
SCRYPT_MAXMEM = 256 * 1024 * 1024

#: an invitation is a single base64url blob; whitespace may appear anywhere
_TOKEN_RE = re.compile(r"csinvite1\.([A-Za-z0-9_\-+/=\s]{16,})")
_WS = re.compile(r"\s+")

#: Messages the UI shows verbatim.
NOT_INVITATION = ("This is not an invitation. Paste the whole text the project owner "
                  "sent you, or open the .csinvite file they sent.")
NEEDS_PASSPHRASE = "This invitation is protected with a passphrase."
WRONG_PASSPHRASE = "Wrong passphrase for this invitation."
NEWER_VERSION = ("This invitation was made by a newer version of CodingStudio. "
                 "Update the app and try again.")
BROKEN = "This invitation is damaged (it may have been cut off by an e-mail program)."


class InviteError(ValueError):
    """A refused invitation; ``code`` is stable, the message is for a person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# encoding helpers
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    s = _WS.sub("", text or "")
    # ``urlsafe_b64decode`` also accepts a mail client's rewritten standard
    # base64 ("+" and "/"); only the padding has to be restored by hand.
    s += "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise InviteError("damaged", BROKEN) from exc


def _canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _scrypt(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=int(n), r=int(r),
                          p=int(p), dklen=DERIVED, maxmem=SCRYPT_MAXMEM)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _mac(enc_header: bytes, nonce: bytes, ct: bytes, mac_key: bytes) -> bytes:
    return hmac.new(mac_key, enc_header + nonce + ct, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

def _build_member(member: dict) -> dict:
    member = member or {}
    out = {
        "username": str(member.get("username") or "").strip(),
        "display_name": str(member.get("display_name") or member.get("username") or "").strip(),
        "invite_id": str(member.get("invite_id") or "").strip(),
        "tags": [str(t) for t in (member.get("tags") or []) if str(t).strip()],
    }
    return out


def build_invite(*, root: str, project: str, member: dict, password: str,
                 passphrase: str | None = None, issued_at: str | None = None) -> str:
    """The invitation text for one coder of one share link.

    *root* is the public WebDAV root (``…/public.php/dav/files/<token>``);
    *password* the link password; *passphrase* protects it with scrypt when
    given (the owner ticks "protect with a passphrase").
    """
    if not str(root or "").strip():
        raise ValueError("an invitation needs the share's public WebDAV root")
    if not password:
        raise ValueError("an invitation needs the share link's password")
    payload: dict = {
        "v": VERSION,
        "kind": KIND,
        "root": str(root).strip().rstrip("/"),
        "project": str(project or "").strip(),
        "member": _build_member(member),
        "issued_at": issued_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if passphrase:
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        key = _scrypt(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
        enc_key, mac_key = key[:32], key[32:]
        raw = password.encode("utf-8")
        ct = bytes(a ^ b for a, b in zip(raw, _keystream(enc_key, nonce, len(raw))))
        secret = {"scrypt": {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": _b64e(salt)},
                  "nonce": _b64e(nonce), "ct": _b64e(ct)}
        mac = _mac(_canonical(payload), nonce, ct, mac_key)
        secret["mac"] = _b64e(mac)
    else:
        secret = {"plain": password}
    payload["secret"] = secret
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return PREFIX + _b64e(blob)


def _run(text: str) -> str | None:
    """The character run after the prefix (it may contain line breaks)."""
    m = _TOKEN_RE.search(str(text or ""))
    return m.group(1) if m else None


def _candidates(text: str) -> list[str]:
    """The run and its plausible shorter forms, longest first.

    A mail client wraps the token (join the whitespace chunks) and may glue the
    following word to the last line ("…Thanks"), so the longest form is tried
    first, then its prefixes cut back a few characters at a time; the first one
    that decodes to an invitation object is the token.
    """
    run = _run(text)
    if run is None:
        return []
    chunks = run.split()
    cands = ["".join(chunks[:k]) for k in range(len(chunks), 0, -1)]
    core = cands[0]
    for cut in range(1, 13):
        if len(core) - cut > 32:
            cands.append(core[: len(core) - cut])
    return cands


def extract_token(text: str) -> str | None:
    """The ``csinvite1.…`` token inside *text* (whitespace removed), else None."""
    for cand in _candidates(text):
        try:
            obj = json.loads(_b64d(cand).decode("utf-8"))
        except (InviteError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "kind" in obj:
            return PREFIX + cand
    return None


def looks_like_invite(text: str) -> bool:
    return PREFIX in str(text or "")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _decode(text: str) -> dict:
    token = extract_token(text)
    if token is None:
        if PREFIX in str(text or ""):
            raise InviteError("damaged", BROKEN)
        raise InviteError("not_invitation", NOT_INVITATION)
    try:
        payload = json.loads(_b64d(token[len(PREFIX):]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, InviteError):
        raise InviteError("damaged", BROKEN) from None
    if not isinstance(payload, dict):
        raise InviteError("not_invitation", NOT_INVITATION)
    version = payload.get("v")
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise InviteError("damaged", BROKEN) from None
    if version > VERSION:
        raise InviteError("newer_version", NEWER_VERSION)
    if version != VERSION:
        raise InviteError("damaged", BROKEN)
    if payload.get("kind") != KIND:
        raise InviteError("not_invitation",
                          "This invitation is for a different kind of project; "
                          "this version of the app cannot open it.")
    return payload


def needs_passphrase(text: str) -> bool:
    """True when the invitation is passphrase-protected (never raises)."""
    try:
        secret = _decode(text).get("secret")
    except InviteError:
        return False
    return isinstance(secret, dict) and "scrypt" in secret and "plain" not in secret


def parse_invite(text: str, passphrase: str | None = None) -> dict:
    """Decode an invitation; returns the normalised fields incl. ``password``.

    Raises :class:`InviteError` with a code the route maps to a friendly
    answer: ``not_invitation``, ``needs_passphrase``, ``wrong_passphrase``,
    ``newer_version``, ``damaged``.
    """
    payload = _decode(text)
    secret = payload.get("secret")
    if not isinstance(secret, dict):
        raise InviteError("damaged", BROKEN)
    if "plain" in secret:
        password = str(secret.get("plain") or "")
    else:
        block = secret.get("scrypt")
        if not isinstance(block, dict):
            raise InviteError("damaged", BROKEN)
        if not passphrase:
            raise InviteError("needs_passphrase", NEEDS_PASSPHRASE)
        try:
            salt = _b64d(block.get("salt") or "")
            nonce = _b64d(secret.get("nonce") or "")
            ct = _b64d(secret.get("ct") or "")
            mac = _b64d(secret.get("mac") or "")
            n, r, p = int(block["n"]), int(block["r"]), int(block["p"])
        except (KeyError, TypeError, ValueError, InviteError):
            raise InviteError("damaged", BROKEN) from None
        if n > SCRYPT_N or r > SCRYPT_R or p > SCRYPT_P:
            # a hostile/absurd cost is refused instead of freezing the app
            raise InviteError("damaged", BROKEN)
        key = _scrypt(passphrase, salt, n, r, p)
        enc_key, mac_key = key[:32], key[32:]
        header = {k: v for k, v in payload.items() if k != "secret"}
        want = _mac(_canonical(header), nonce, ct, mac_key)
        if not hmac.compare_digest(want, mac):
            raise InviteError("wrong_passphrase", WRONG_PASSPHRASE)
        password = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct)))
                         ).decode("utf-8", "replace")
    if not password:
        raise InviteError("damaged", BROKEN)
    member = payload.get("member") if isinstance(payload.get("member"), dict) else {}
    username = str(member.get("username") or "").strip()
    if not username:
        raise InviteError("damaged", BROKEN)
    return {
        "v": VERSION,
        "kind": KIND,
        "root": str(payload.get("root") or "").strip().rstrip("/"),
        "project": str(payload.get("project") or "").strip(),
        "member": {
            "username": username,
            "display_name": str(member.get("display_name") or username),
            "invite_id": str(member.get("invite_id") or ""),
            "tags": [str(t) for t in (member.get("tags") or [])],
        },
        "password": password,
        "issued_at": str(payload.get("issued_at") or ""),
        "protected": "plain" not in secret,
    }


def describe(text: str) -> dict:
    """What the connect dialog may show before the coder types a passphrase."""
    payload = _decode(text)
    secret = payload.get("secret") if isinstance(payload.get("secret"), dict) else {}
    member = payload.get("member") if isinstance(payload.get("member"), dict) else {}
    return {
        "project": str(payload.get("project") or ""),
        "root": str(payload.get("root") or "").rstrip("/"),
        "display_name": str(member.get("display_name") or member.get("username") or ""),
        "username": str(member.get("username") or ""),
        "needs_passphrase": "plain" not in secret,
    }


# ---------------------------------------------------------------------------
# the text the owner pastes into an e-mail
# ---------------------------------------------------------------------------

def instructions(project: str, display_name: str, root: str, *, passphrase: str | None = None) -> str:
    """Plain-language setup text for one coder (no secret in it)."""
    who = display_name or "you"
    lines = [
        f"Hello {who},",
        "",
        f"you are invited to code in the CodingStudio project \"{project or 'the project'}\".",
        "You do not need a Nextcloud account for this: the link below opens one folder only.",
        "",
        "1. Install and start CodingStudio.",
        "2. In the project dialog choose \"Open online project\" and then \"Join with an invitation\".",
        "3. Paste the invitation text below (or drop the .csinvite file you received).",
    ]
    if passphrase:
        lines.append("4. Type the passphrase you were given separately.")
    lines += [
        "",
        "The invitation is personal: do not forward it. If you lose it, ask the project owner",
        "for a new one.",
        "",
        "Invitation:",
    ]
    return "\n".join(lines)


def invitation_filename(project: str, display_name: str) -> str:
    """``CodingStudio <project> - <name>.csinvite`` (Windows-safe)."""
    def clean(value: str) -> str:
        s = "".join(c if (c.isalnum() or c in " ._-()") else "_" for c in str(value or ""))
        return s.strip(" .") or "invitation"
    return f"CodingStudio {clean(project)} - {clean(display_name)}.csinvite"
