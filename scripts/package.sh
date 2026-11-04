#!/usr/bin/env bash
#
# Turn the per-platform build directories into the zips a customer downloads.
#
#   package.sh <build-dir> <out-dir> <service-name> <entry-script>
#
# Lives in a script rather than inline in the workflow so the same code runs in
# CI and locally. docker-local/build-binary.sh calls this after simulating the
# artifact round-trip, which is the only way to test it: what this repairs is
# damage done between the build job and this one, and a local copy of the logic
# would only ever test the copy.
#
# What it repairs, and why it does not trust what it is given:
#
#   * The executable bit. actions/upload-artifact cannot preserve it -- the
#     format has no place for it -- so the binary arrives as 0644 and would zip
#     as 0644. The customer then unzips something they cannot run, and no
#     privilege fixes it: Linux requires at least one x bit even for root.
#
# Release prod-1 shipped without the executable bit, having passed every job.
#
# There is deliberately no logs/ directory. Services do not write log files:
# they print, the orchestrator tees every child's output to
# project/logging/<service>, and logging-service is the only thing that writes
# to disk -- under its own configured directory. A logs/ folder in every
# service would be an empty directory nothing ever opens.

set -euo pipefail

BUILD_DIR="${1:?usage: package.sh <build-dir> <out-dir> <service> <entry-script>}"
OUT_DIR="${2:?missing out-dir}"
SERVICE="${3:?missing service name}"
SCRIPT_PATH="${4:?missing entry script}"

binary="$(basename "$SCRIPT_PATH" .py)"
mkdir -p "$OUT_DIR"

# Resolved to an absolute path HERE, once, before anything changes directory.
# The zip below runs in a subshell that has cd'd into the build directory, so a
# relative out-dir would be resolved against THAT - and the workflow passes a
# relative one ("upload"). Getting this wrong does not fail loudly: the
# substitution yields nothing and zip tries to write to the filesystem root.
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

shopt -s nullglob

found=0
for dir in "$BUILD_DIR"/build-*; do
  [ -d "$dir" ] || continue
  platform="$(basename "$dir")"; platform="${platform#build-}"

  for candidate in "$dir/$binary" "$dir/$binary.exe"; do
    [ -f "$candidate" ] && chmod +x "$candidate"
  done

  # And anything else meant to be run. A service that ships an installer
  # alongside its binary loses ITS executable bit to the same artifact upload,
  # and the customer finds a script they cannot run for the same reason they
  # once found a binary they could not.
  for script in "$dir"/*.sh; do
    [ -f "$script" ] && chmod +x "$script"
  done

  ( cd "$dir" && zip -qr "${OUT_DIR}/${SERVICE}-${platform}.zip" . )
  found=$((found + 1))
done

# A silent zero would publish a release with no binaries in it, and
# `gh release create` succeeds perfectly well with no files.
if [ "$found" -eq 0 ]; then
  echo "::error::no platform artifacts found in ${BUILD_DIR} - nothing to release"
  exit 1
fi

echo "Packaged ${found} platform(s):"
ls -lh "$OUT_DIR"
