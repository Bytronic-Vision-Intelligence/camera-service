#!/usr/bin/env bash
#
# Run .github/workflows/ locally.
#
#   ./run.sh -l                 list jobs
#   ./run.sh --dryrun           show execution order without running anything
#   ./run.sh                    run the checks a pull request would run
#   ./run.sh --release          build the binary and smoke-test it
#   ./run.sh --all              checks, then release
#   ./run.sh --release-workflow drive the release workflow through act (limited,
#                               see readme.md - the vendor action does not run
#                               under act)
#   ./run.sh --verify-release [tag]
#                               check a PUBLISHED release as a customer gets it
#   ./run.sh -j build           run one job
#   ./run.sh pull_request       run a different event
#   ./run.sh --fresh            wipe the toolcache first, resolve deps from scratch
#
# Every run is tee'd to logs/ — the container is --rm, so without this the
# output dies with it.
#
# windows-latest legs execute on a Linux image — ordering is real, the OS isn't.
#
# This folder is repo-agnostic: PROJECT_NAME is derived from the directory
# name, so it drops into any service repo unedited.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

REPO_ROOT="$(cd .. && pwd)"
export REPO_ROOT

PROJECT_NAME="$(basename "$REPO_ROOT")"
export PROJECT_NAME

if [ "${1:-}" = "--fresh" ]; then
  shift
  # act keeps /opt/hostedtoolcache in a named volume, and pip installs into it.
  # Without this, a run can pass on packages a previous run left behind.
  docker volume rm -f act-toolcache >/dev/null 2>&1 || true
  echo "==> wiped act-toolcache: this run resolves every dependency from scratch"
fi

# --release and --all select which workflows the entrypoint picks up. Release
# runs the BUILD job only: signing needs a key that is not on this machine, and
# publishing would cut a real GitHub release from a working tree.
# --release does NOT go through act. The release build calls a vendor action
# that runs a nested Docker step expecting its own files at /github/action/,
# which act does not mount, so it dies before building anything. build-binary.sh
# reproduces the same PyInstaller invocation directly instead - which is the
# check that matters, because the failure mode is a binary that builds cleanly
# and dies on its first import.
# run.sh has already cd'd to this directory.
run_binary_build() { ./build-binary.sh; }

# Not a mode: it inspects a published release rather than running a workflow.
if [ "${1:-}" = "--verify-release" ]; then
  shift
  exec ./verify-release.sh "$@"
fi

MODES=(checks)
case "${1:-}" in
  --release)          shift; MODES=(binary) ;;
  --all)              shift; MODES=(checks binary) ;;
  --release-workflow) shift; MODES=(release) ;;
esac

# Release mode builds from a clean copy of the TRACKED tree, never the live
# working directory.
#
# Two reasons, both learned the hard way. The mount is read-write, so a build
# action running `uv venv` in the repo root collides with the .venv a developer
# runs pytest in - "A virtual environment already exists" - a failure CI never
# has, because actions/checkout starts from nothing. And the same mount means
# anything the build writes, or clears, lands in the working tree.
#
# Tracked files copied from the working tree, so uncommitted edits ARE tested;
# ignored paths (.venv, dist, __pycache__) are not there to collide.
if [ "${MODES[0]}" = release ]; then
  TREE="$PWD/.tree"
  rm -rf "$TREE"; mkdir -p "$TREE"
  (cd "$REPO_ROOT" && git ls-files -z | xargs -0 tar -cf -) | tar -xf - -C "$TREE"

  untracked=$(cd "$REPO_ROOT" && git ls-files --others --exclude-standard | wc -l | tr -d ' ')
  if [ "$untracked" -gt 0 ]; then
    echo "==> note: $untracked untracked file(s) are NOT in the release build."
    echo "    Only tracked files are copied, because that is what CI checks out."
  fi
  export REPO_ROOT="$TREE"
fi

mkdir -p artifacts logs

LOG="logs/act-$(date +%Y%m%d-%H%M%S).log"
ln -sf "$(basename "$LOG")" logs/latest.log

rc=0
for mode in "${MODES[@]}"; do
  [ "${#MODES[@]}" -gt 1 ] && echo "==================== $mode ===================="

  if [ "$mode" = binary ]; then
    run_binary_build || rc=$?
    continue
  fi

  # Strip ANSI + CR so the saved log is greppable; the terminal still gets colour.
  MODE="$mode" docker compose run --rm --build -e MODE="$mode" act "$@" 2>&1 \
    | tee -a >(perl -pe 's/\e\[[0-9;]*m//g; s/\r//g' >> "$LOG")
  [ "${PIPESTATUS[0]}" -eq 0 ] || rc="${PIPESTATUS[0]}"
done

exit "$rc"
