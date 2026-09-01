#!/usr/bin/env bash
#
# Thin wrapper around act. Everything after `run.sh` is passed straight through,
# so any act flag works: -l, --dryrun, -j <job>, -v, etc.

set -euo pipefail

WORKFLOWS="${WORKFLOWS_DIR:-.github/workflows}"

# windows-latest is mapped onto a Linux image so the job graph still resolves
# and you can watch the ordering. That leg does NOT prove anything about
# Windows — only GitHub's real runner does.
act_once() {
  act \
    --concurrent-jobs 1 \
    -W "$WORKFLOWS" \
    -P ubuntu-latest=catthehacker/ubuntu:act-latest \
    -P ubuntu-24.04=catthehacker/ubuntu:act-24.04 \
    -P ubuntu-22.04=catthehacker/ubuntu:act-22.04 \
    -P windows-latest=catthehacker/ubuntu:act-latest \
    --container-architecture linux/amd64 \
    --artifact-server-path /artifacts \
    --rm \
    "$@"
}

# One act invocation per matrix leg.
#
# act shares a single act-toolcache volume across job containers and mounts it
# at /opt/hostedtoolcache. Two legs installing at once write over each other
# there, which surfaces as a torn install - "No such file or directory:
# .../site-packages/nvidia/__init__.py", or a half-written .so as "Fatal Python
# error: Bus error". --concurrent-jobs does NOT prevent this: it caps concurrent
# *jobs*, and the legs of one matrix are a single job to act. Running each leg
# as its own act invocation is what actually serialises them. Real GitHub gives
# every leg its own runner, so this only constrains the local mirror.
#
# The legs are read out of the workflow rather than hardcoded, so this tracks
# whatever the remote CI runs.
legs() {
  yq -r '
    .jobs[]
    | select(.strategy.matrix)
    | .strategy.matrix
    | to_entries[]
    | select(.key != "include" and .key != "exclude")
    | .key as $k | .value[] | "\($k):\(.)"
  ' "$WORKFLOWS"/*.y*ml 2>/dev/null | sort -u
}

# Pass straight through when nothing is executed (-l/--dryrun), or when the
# caller has already pinned a leg themselves.
for arg in "$@"; do
  case "$arg" in
    -l|--list|--dryrun|-n|--matrix) act_once "$@"; exit $? ;;
  esac
done

mapfile -t LEGS < <(legs)

# Distinct matrix keys: with two or more the legs are a cartesian product and
# pinning one key still leaves the others expanding in parallel, so hand it to
# act unsplit rather than pretend otherwise.
KEYS=$(printf '%s\n' "${LEGS[@]}" | cut -d: -f1 | sort -u | grep -c . || true)

if [ "${#LEGS[@]}" -eq 0 ] || [ "$KEYS" -ne 1 ]; then
  [ "$KEYS" -gt 1 ] && echo "==> matrix has $KEYS dimensions; running unsplit (legs may race on the toolcache)"
  act_once "$@"; exit $?
fi

echo "==> ${#LEGS[@]} matrix legs, one act run each: ${LEGS[*]}"
rc=0
for leg in "${LEGS[@]}"; do
  echo "==> leg: $leg"
  # fail-fast: false in the workflow, so run every leg and report the worst.
  act_once --matrix "$leg" "$@" || rc=$?
done
exit "$rc"
