"""Sign a release artefact with the shared SERVICE signing key.

Runs in CI, once per release, and nowhere else. The signature it produces is
what the orchestrator checks before running a service binary, so this is the
last point at which a wrong key can be caught cheaply: downstream, a artefact
signed by the wrong key is indistinguishable from a tampered one, and the
report lands on a customer machine pointing at a binary that was never at
fault.

That is why EXPECTED_PUBLIC_KEY exists. The private half lives in a secret,
one copy per service repository, and a secret pasted from the wrong place is
silent. Deriving the public half here and comparing it against a value
committed in the open turns that into a build failure with a name attached.
"""
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

#: Hex of the raw 32-byte public half of the SERVICE signing key -- the one
#: shared across every service repository and trusted by the orchestrator.
#:
#: This is bvi-svc-2026-09. Its private half is held as SERVICE_SIGNING_KEY in
#: every repository that signs a binary, and nowhere else.
#:
#: It is NOT the orchestrator key. That one signs the orchestrator binary,
#: lives in a single private repository, and is what release-service trusts to
#: mean "genuinely ours". This one is shared across eight repositories, three
#: of them public, so a key that was both would let any service repository
#: forge an orchestrator. Keeping them apart is the point of the three-key
#: split.
EXPECTED_PUBLIC_KEY = "8af4436a6ed698c33419fc781dd946db03eb7e712fc551dfc46d4ce925c31ee8"


def _signing_key() -> Ed25519PrivateKey:
    """The key named by SERVICE_SIGNING_KEY, if it is the one we expect.

    Raises:
        SystemExit: naming which of the four things is wrong -- no key, not
            hex, wrong length, or the wrong key entirely. The fix differs for
            each: set the secret, re-paste it, re-paste it, or find out which
            key this repository was given.
    """
    if not EXPECTED_PUBLIC_KEY:
        raise SystemExit(
            "EXPECTED_PUBLIC_KEY is unset in scripts/sign.py, so there is "
            "nothing to check the signing key against - record the SERVICE "
            "key's public half there before signing anything")

    raw = (os.environ.get("SERVICE_SIGNING_KEY") or "").strip()
    if not raw:
        raise SystemExit(
            "SERVICE_SIGNING_KEY is not set - add the SERVICE signing key as "
            "a repository secret")
    try:
        material = bytes.fromhex(raw)
    except ValueError:
        raise SystemExit(
            "SERVICE_SIGNING_KEY is not hex - paste the value keygen.py "
            "printed, with no prefix and no quotes")
    if len(material) != 32:
        raise SystemExit(
            f"SERVICE_SIGNING_KEY must be 32 bytes of hex, got {len(material)}")

    private = Ed25519PrivateKey.from_private_bytes(material)
    public = private.public_key().public_bytes_raw().hex()
    if public != EXPECTED_PUBLIC_KEY:
        raise SystemExit(
            "SERVICE_SIGNING_KEY does not match the key this repository "
            f"expects.\n  expected public half: {EXPECTED_PUBLIC_KEY}\n"
            f"  secret's public half: {public}\n"
            "Either the secret was pasted from the wrong place, or the key "
            "was rotated here and not in scripts/sign.py.")
    return private


def sign_file(path: Path) -> Path:
    """Write a detached signature beside `path` and return where it went.

    The signature covers the file's bytes and nothing else -- not its name,
    not its length prefix -- so a consumer who renames the download can still
    verify it.

    Raises:
        SystemExit: when the key is unusable or the artefact is missing.
    """
    private = _signing_key()
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"no such file to sign: {path} ({exc})")

    signature = private.sign(payload)
    # Verify what was just produced. A signature this process cannot check is
    # one nothing downstream can either, and the cost of finding out here
    # rather than on a customer machine is one line.
    private.public_key().verify(signature, payload)

    written = path.parent / (path.name + ".sig")
    written.write_bytes(signature)
    return written


def main(argv=None) -> int:
    paths = list(argv if argv is not None else sys.argv[1:])
    if not paths:
        raise SystemExit("usage: sign.py <artefact> [artefact ...]")
    for path in paths:
        print(f"signed {sign_file(Path(path))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
