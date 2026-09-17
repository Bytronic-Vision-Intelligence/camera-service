# test/test_sign.py
"""Signing a release artefact with the shared SERVICE key.

The signature this produces is checked by the orchestrator before it runs a
service binary. Nothing downstream of here can tell a correctly signed
artefact from one signed by the wrong key -- it can only report that some key
it does not trust was used, months later, on a customer machine. So the
checks that belong here are the ones that cannot be made anywhere else.
"""
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

import sign

PAYLOAD = b"PK\x03\x04 a zip, as far as anyone here is concerned"


@pytest.fixture
def key(monkeypatch):
    """A throwaway signing key, and the constant that says we expect it."""
    private = Ed25519PrivateKey.generate()
    monkeypatch.setenv("SERVICE_SIGNING_KEY", private.private_bytes_raw().hex())
    monkeypatch.setattr(
        sign, "EXPECTED_PUBLIC_KEY",
        private.public_key().public_bytes_raw().hex())
    return private


@pytest.fixture
def artefact(tmp_path):
    path = tmp_path / "a-service-linux-amd64.zip"
    path.write_bytes(PAYLOAD)
    return path


def test_it_writes_a_signature_beside_the_artefact(key, artefact):
    written = sign.sign_file(artefact)
    assert written == artefact.with_suffix(artefact.suffix + ".sig")
    assert written.is_file()


def test_the_signature_verifies_under_the_expected_key(key, artefact):
    written = sign.sign_file(artefact)
    Ed25519PublicKey.from_public_bytes(
        key.public_key().public_bytes_raw()
    ).verify(written.read_bytes(), PAYLOAD)


def test_the_signature_is_raw_detached_bytes(key, artefact):
    """64 raw bytes, no envelope, no base64. The orchestrator will pass these
    straight to verify(); anything else fails there rather than here."""
    assert len(sign.sign_file(artefact).read_bytes()) == 64


def test_it_signs_the_artefact_and_not_its_name(key, artefact, tmp_path):
    """Two artefacts with identical bytes and different names must produce the
    same signature. A signer that mixed the filename in would make a signature
    unverifiable by anyone who renamed the download."""
    other = tmp_path / "renamed.zip"
    other.write_bytes(PAYLOAD)
    assert sign.sign_file(artefact).read_bytes() == sign.sign_file(other).read_bytes()


def test_a_missing_signing_key_is_refused(key, monkeypatch, artefact):
    # `key` first, so EXPECTED_PUBLIC_KEY is set and this isolates the absent
    # secret rather than tripping the earlier check.
    monkeypatch.delenv("SERVICE_SIGNING_KEY", raising=False)
    with pytest.raises(SystemExit, match="SERVICE_SIGNING_KEY"):
        sign.sign_file(artefact)


def test_a_signing_key_that_is_not_hex_is_refused(key, monkeypatch, artefact):
    monkeypatch.setenv("SERVICE_SIGNING_KEY", "not hex at all")
    with pytest.raises(SystemExit, match="hex"):
        sign.sign_file(artefact)


def test_a_signing_key_of_the_wrong_length_is_refused(key, monkeypatch, artefact):
    monkeypatch.setenv("SERVICE_SIGNING_KEY", "ab" * 16)
    with pytest.raises(SystemExit, match="32 bytes"):
        sign.sign_file(artefact)


def test_a_key_that_is_not_the_expected_one_is_refused(key, monkeypatch, artefact):
    """The whole point of this module. A repo whose SERVICE_SIGNING_KEY was
    pasted from the wrong place signs perfectly valid artefacts that the
    orchestrator then refuses -- with a message about an untrusted key, on a
    customer machine, pointing at a binary that was never at fault. Nothing
    downstream can distinguish that from tampering. It has to fail here."""
    stranger = Ed25519PrivateKey.generate()
    monkeypatch.setenv("SERVICE_SIGNING_KEY", stranger.private_bytes_raw().hex())
    with pytest.raises(SystemExit, match="does not match the key this repository expects"):
        sign.sign_file(artefact)
    assert not artefact.with_suffix(".zip.sig").exists()


def test_signing_is_refused_when_no_key_is_expected(key, monkeypatch, artefact):
    """EXPECTED_PUBLIC_KEY ships unset. Signing anyway would produce artefacts
    nobody can attribute, and would quietly disable the check above."""
    monkeypatch.setattr(sign, "EXPECTED_PUBLIC_KEY", "")
    with pytest.raises(SystemExit, match="EXPECTED_PUBLIC_KEY"):
        sign.sign_file(artefact)


def test_a_missing_artefact_is_refused(key, tmp_path):
    with pytest.raises(SystemExit, match="no such file"):
        sign.sign_file(tmp_path / "never-built.zip")


#: The team's SERVICE signing key, whose private half is held as
#: SERVICE_SIGNING_KEY in every repository that signs a binary.
SHIPPED_KEY = "8af4436a6ed698c33419fc781dd946db03eb7e712fc551dfc46d4ce925c31ee8"


def test_the_expected_key_is_the_team_key():
    """A value here is trusted by every service that copies this template, so
    it may only ever be the team's real key. Pinned exactly rather than merely
    "not empty": a placeholder, a truncated paste or somebody's local test key
    would all satisfy the looser check."""
    assert sign.EXPECTED_PUBLIC_KEY == SHIPPED_KEY


def test_the_expected_key_is_a_usable_ed25519_public_key():
    """A hand-pasted 64-hex string is one character away from bytes that are
    the wrong length or not a point on the curve. Caught here, it costs
    nothing; caught at release time it stops a build for reasons that read
    like a key mismatch."""
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(sign.EXPECTED_PUBLIC_KEY))


def test_the_expected_key_is_not_the_orchestrator_key():
    """The orchestrator key signs the orchestrator binary, lives in one private
    repository, and is what release-service trusts to mean "genuinely ours".
    This key is shared across eight repositories, three of them public. One key
    serving both would let any service repository forge an orchestrator -- and
    the orchestrator binary is the one signature no customer can check."""
    ORCHESTRATOR_KEY = "40f59afe5701eac2e4084e7a9cc0fcd586c7a576908e8e534248f9d1a264d760"
    assert sign.EXPECTED_PUBLIC_KEY != ORCHESTRATOR_KEY
