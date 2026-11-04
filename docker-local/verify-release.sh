#!/usr/bin/env bash
#
# Verify a PUBLISHED release the way a customer receives it.
#
#   ./verify-release.sh              # the latest release
#   ./verify-release.sh prod-1       # a specific tag
#
# This is the only check that sees what actually ships. The build tests a
# binary before it is uploaded; `build-binary.sh` never goes through an
# artifact at all. Release prod-1 passed every job and shipped a binary the
# customer could not execute and a `logs/` directory that was not there, both
# lost in the artifact round-trip between the build and the packaging job.
#
# For each platform it checks:
#   * the detached signature verifies under the key committed in scripts/sign.py
#   * the zip's SHA256 matches SHA256SUMS
#   * the binary inside is EXECUTABLE
#   * config.yaml is present, and no empty logs/ is shipped
#
# gh runs on the host, for its credentials; the signature check runs in a
# container, so nothing needs installing here.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TAG="${1:-}"
# Explicitly `origin`. This repo also has an `upstream` remote pointing at the
# template it was forked from, and a bare `gh repo view` resolves to that one --
# which then reports "release not found" against somebody else's repository.
# Parsed with ERE only: BSD and GNU sed disagree on anything richer.
REPO="$(git -C .. remote get-url origin | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
KEY=$(grep -oE 'EXPECTED_PUBLIC_KEY = "[0-9a-fA-F]*"' ../scripts/sign.py | grep -oE '[0-9a-fA-F]{64}' || true)
[ -n "$KEY" ] || { echo "FAIL: scripts/sign.py has no EXPECTED_PUBLIC_KEY to check against"; exit 1; }

WORK="$PWD/.release-check"
rm -rf "$WORK"; mkdir -p "$WORK"

echo "==> repo: $REPO"
if [ -n "$TAG" ]; then
  echo "==> tag:  $TAG"
  gh release download "$TAG" --repo "$REPO" --dir "$WORK" --clobber
else
  TAG=$(gh release view --repo "$REPO" --json tagName --jq .tagName)
  echo "==> tag:  $TAG (latest)"
  gh release download --repo "$REPO" --dir "$WORK" --clobber
fi

shopt -s nullglob
zips=("$WORK"/*.zip)
[ "${#zips[@]}" -gt 0 ] || { echo "FAIL: the release has no zips"; exit 1; }

cp ../scripts/sign.py "$WORK/.sign.py" 2>/dev/null || true
cat > "$WORK/.check.py" <<'INNER'
import hashlib, pathlib, sys, zipfile, stat
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

work, key_hex = pathlib.Path("/w"), sys.argv[1]
key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
sums = {}
sumfile = work / "SHA256SUMS"
if sumfile.is_file():
    for line in sumfile.read_text().splitlines():
        if line.strip():
            digest, name = line.split()
            sums[name] = digest

failures = []
for zpath in sorted(work.glob("*.zip")):
    name = zpath.name
    blob = zpath.read_bytes()
    print(f"\n{name}")

    sig = zpath.with_suffix(".zip.sig")
    if not sig.is_file():
        failures.append(f"{name}: no .sig published"); print("  signature   MISSING"); continue
    try:
        key.verify(sig.read_bytes(), blob)
        print("  signature   ok")
    except InvalidSignature:
        failures.append(f"{name}: signature does not verify"); print("  signature   INVALID")

    digest = hashlib.sha256(blob).hexdigest()
    if sums.get(name) == digest:
        print("  checksum    ok")
    else:
        failures.append(f"{name}: checksum mismatch"); print("  checksum    MISMATCH")

    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        # A zip stores the unix mode in the top 16 bits of external_attr.
        #
        # Directories are excluded, and that is the whole point: a directory
        # always carries the x bit, so counting them made this check pass on any
        # zip containing `logs/` -- including one whose binary was 0644, which
        # is exactly the defect this exists to catch.
        execs = [i.filename for i in z.infolist()
                 if not i.is_dir() and (i.external_attr >> 16) & stat.S_IXUSR]
        if execs:
            print(f"  executable  ok ({', '.join(execs)})")
        else:
            failures.append(f"{name}: nothing in the zip is executable")
            print("  executable  NONE - the customer cannot run this")
        for needed in ("config.yaml",):
            ok = any(n.endswith(needed) for n in names)
            print(f"  {needed:<11} {'ok' if ok else 'MISSING'}")
            if not ok:
                failures.append(f"{name}: {needed} missing")
        # Absent by design: logging goes over MQTT. A logs/ here means a
        # service package is shipping an empty directory nothing opens.
        has_logs = any(n.startswith("logs") for n in names)
        print(f"  no logs/    {'NO - empty dir shipped' if has_logs else 'ok'}")
        if has_logs:
            failures.append(f"{name}: ships an empty logs/ directory")

print()
if failures:
    print("FAIL:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASS: every published artefact verifies and is usable as shipped.")
INNER

docker run --rm -v "$WORK:/w" python:3.10-slim bash -c \
  "pip install -q cryptography >/dev/null 2>&1 && python /w/.check.py '$KEY'"
