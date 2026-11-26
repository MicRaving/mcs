"""Invitation tokens (``online/invite.py``) - the share-link coder's way in.

An invitation carries both halves of a share-link join: the public WebDAV root
of the link, the app identity the owner set up in ``members.json``, and (in its
secret) the link password - plain by default, scrypt-protected when the owner
ticks "protect with a passphrase".  These tests pin the format, the crypto
round trip, the refusals and the forgiving parse of an e-mail-wrapped token.
"""

from __future__ import annotations

import base64
import json

import pytest

from online import invite


ROOT = "https://cloud.example.org/public.php/dav/files/AbC123token"
MEMBER = {"username": "anna", "display_name": "Anna B.", "invite_id": "inv-42",
          "tags": ["AN", "AN2"]}


def _plain() -> str:
    return invite.build_invite(root=ROOT, project="Meta analysis", member=MEMBER,
                               password="L1nk-p@ssw0rd!x")


def _structured() -> str:
    return invite.build_invite(root=ROOT, project="Meta analysis", member=MEMBER,
                               password="L1nk-p@ssw0rd!x", passphrase="open sesame")


# ---------------------------------------------------------------------------
# the shape of the token
# ---------------------------------------------------------------------------

def test_the_token_is_the_documented_format():
    text = _plain()
    assert text.startswith("csinvite1.")
    raw = base64.urlsafe_b64decode(text[len("csinvite1."):] + "===")
    payload = json.loads(raw.decode("utf-8"))
    assert payload["v"] == 1
    assert payload["kind"] == "nc-link"
    assert payload["root"] == ROOT
    assert payload["project"] == "Meta analysis"
    assert payload["member"] == {"username": "anna", "display_name": "Anna B.",
                                 "invite_id": "inv-42", "tags": ["AN", "AN2"]}
    assert payload["secret"] == {"plain": "L1nk-p@ssw0rd!x"}
    assert payload["issued_at"]
    # the token is safe to put in an e-mail: no "+", "/" or "="
    body = text[len("csinvite1."):]
    assert "+" not in body and "/" not in body and "=" not in body


def test_plain_roundtrip_and_describe():
    inv = invite.parse_invite(_plain())
    assert inv["password"] == "L1nk-p@ssw0rd!x"
    assert inv["root"] == ROOT
    assert inv["project"] == "Meta analysis"
    assert inv["member"]["username"] == "anna"
    assert inv["member"]["tags"] == ["AN", "AN2"]
    assert inv["protected"] is False
    desc = invite.describe(_plain())
    assert desc == {"project": "Meta analysis", "root": ROOT,
                    "display_name": "Anna B.", "username": "anna",
                    "needs_passphrase": False}


def test_passphrase_roundtrip_never_contains_the_password():
    text = _structured()
    assert "L1nk-p@ssw0rd!x" not in text
    assert invite.needs_passphrase(text) is True
    inv = invite.parse_invite(text, "open sesame")
    assert inv["password"] == "L1nk-p@ssw0rd!x"
    assert inv["protected"] is True
    assert invite.needs_passphrase(_plain()) is False


def test_the_scrypt_block_uses_only_the_stdlib_and_a_stated_cost():
    payload = json.loads(base64.urlsafe_b64decode(_structured()[len("csinvite1."):] + "==="))
    block = payload["secret"]["scrypt"]
    assert block["n"] == invite.SCRYPT_N and block["r"] == invite.SCRYPT_R
    assert block["p"] == invite.SCRYPT_P and block["salt"]
    assert payload["secret"]["nonce"] and payload["secret"]["ct"] and payload["secret"]["mac"]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

def test_a_wrong_passphrase_is_refused():
    with pytest.raises(invite.InviteError) as exc:
        invite.parse_invite(_structured(), "not the passphrase")
    assert exc.value.code == "wrong_passphrase"
    assert "passphrase" in str(exc.value).lower()


def test_a_protected_invitation_asks_for_the_passphrase_first():
    with pytest.raises(invite.InviteError) as exc:
        invite.parse_invite(_structured())
    assert exc.value.code == "needs_passphrase"


def test_a_tampered_ciphertext_or_mac_is_refused_before_decrypting():
    text = _structured()
    payload = json.loads(base64.urlsafe_b64decode(text[len("csinvite1."):] + "==="))

    for field, value in (("ct", invite._b64e(b"\x00" * 20)),
                         ("mac", invite._b64e(b"\x01" * 32)),
                         ("nonce", invite._b64e(b"\x02" * 16))):
        broken = json.loads(json.dumps(payload))
        broken["secret"][field] = value
        blob = "csinvite1." + invite._b64e(json.dumps(broken).encode())
        with pytest.raises(invite.InviteError) as exc:
            invite.parse_invite(blob, "open sesame")
        assert exc.value.code == "wrong_passphrase", field
    # the untouched payload still works (the test itself did not break it)
    assert invite.parse_invite(text, "open sesame")["password"] == "L1nk-p@ssw0rd!x"


def test_a_tampered_header_is_refused_too():
    """The MAC covers the header, so a rewritten identity is not accepted."""
    text = _structured()
    payload = json.loads(base64.urlsafe_b64decode(text[len("csinvite1."):] + "==="))
    payload["member"]["username"] = "mallory"
    blob = "csinvite1." + invite._b64e(json.dumps(payload).encode())
    with pytest.raises(invite.InviteError) as exc:
        invite.parse_invite(blob, "open sesame")
    assert exc.value.code == "wrong_passphrase"


def test_not_an_invitation_and_a_newer_version():
    with pytest.raises(invite.InviteError) as exc:
        invite.parse_invite("please join my project, here is a link: https://x/s/abc")
    assert exc.value.code == "not_invitation"
    assert "not an invitation" in str(exc.value).lower()

    payload = json.loads(base64.urlsafe_b64decode(_plain()[len("csinvite1."):] + "==="))
    payload["v"] = 2
    newer = "csinvite1." + invite._b64e(json.dumps(payload).encode())
    with pytest.raises(invite.InviteError) as exc:
        invite.parse_invite(newer)
    assert exc.value.code == "newer_version"
    assert "newer version" in str(exc.value).lower()


def test_a_truncated_token_is_reported_as_damaged_not_as_a_wrong_type():
    text = _plain()
    with pytest.raises(invite.InviteError) as exc:
        invite.parse_invite(text[: len(text) // 2])
    assert exc.value.code == "damaged"


# ---------------------------------------------------------------------------
# the forgiving parse (mail clients wrap and surround the token)
# ---------------------------------------------------------------------------

def test_an_email_wrapped_token_is_found_with_its_surrounding_prose():
    text = _plain()
    body = text[len("csinvite1."):]
    wrapped = "csinvite1." + body[:70] + "\r\n   " + body[70:]
    mail = ("Hello Anna,\n\nhere is your invitation:\n\n" + wrapped
            + "\n\nSee you!\nBetty\n")
    assert invite.extract_token(mail) == text
    assert invite.parse_invite(mail)["password"] == "L1nk-p@ssw0rd!x"
    assert invite.looks_like_invite(mail) is True
    assert invite.looks_like_invite("nothing here") is False


def test_a_token_with_a_trailing_word_on_the_same_line_is_cut_back():
    text = _plain()
    # some clients join the last line with the following word: "…Thanks"
    joined = text + "Thanks"
    assert invite.extract_token(joined) == text


# ---------------------------------------------------------------------------
# the owner's e-mail text (no secret in it)
# ---------------------------------------------------------------------------

def test_instructions_are_plain_language_and_carry_no_secret():
    text = invite.instructions("Meta analysis", "Anna B.", ROOT)
    assert "CodingStudio" in text and "Join with an invitation" in text
    assert "Anna B." in text
    assert "L1nk-p@ssw0rd!x" not in text and "csinvite1." not in text
    protected = invite.instructions("Meta", "Anna", ROOT, passphrase="x")
    assert "passphrase" in protected.lower()


def test_the_invitation_file_name_is_windows_safe():
    name = invite.invitation_filename("Meta: analysis", "Anna/B.")
    assert name == "CodingStudio Meta_ analysis - Anna_B.csinvite"
    for bad in '\\/:*?"<>|':
        assert bad not in name.replace(".csinvite", "")
