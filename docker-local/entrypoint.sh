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
  local workflow="$1"; shift
  act \
    --concurrent-jobs 1 \
    -W "$workflow" \
    -P ubuntu-latest=catthehacker/ubuntu:act-latest \
    -P ubuntu-24.04=catthehacker/ubuntu:act-24.04 \
    -P ubuntu-22.04=catthehacker/ubuntu:act-22.04 \
    -P windows-latest=catthehacker/ubuntu:act-latest \
    --container-architecture linux/amd64 \
    --artifact-server-path /artifacts \
    --rm \
    "$@"
}

# Which workflows to run: the ones a pull request would trigger.
#
# act filters by event, not by branch, so pointing it at the whole directory
# runs release and deployment workflows too - a local check would try to build
# production binaries and cut a GitHub release from a developer's checkout.
# Selecting on pull_request matches what this runner is actually for: "would my
# PR pass". Set WORKFLOWS_DIR to override and run something specific.
ci_workflows() {
  local f
  for f in "$WORKFLOWS"/*.y*ml; do
    [ -e "$f" ] || continue
    # `on` is a YAML 1.1 boolean, so a parser may key it as "true" instead.
    if yq -e '(.on // ."true") | has("pull_request")' "$f" >/dev/null 2>&1; then
      printf '%s\n' "$f"
    fi
  done
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
  ' "$1" 2>/dev/null | sort -u
}

mapfile -t WORKFLOW_FILES < <(ci_workflows)
if [ "${#WORKFLOW_FILES[@]}" -eq 0 ]; then
  # Nothing declares pull_request. Hand act the directory rather than silently
  # running nothing.
  echo "==> no workflow triggers on pull_request; running $WORKFLOWS unfiltered"
  WORKFLOW_FILES=("$WORKFLOWS")
fi

# Pass straight through when nothing is executed (-l/--dryrun), or when the
# caller has already pinned a leg themselves.
passthrough=0
for arg in "$@"; do
  case "$arg" in
    -l|--list|--dryrun|-n|--matrix) passthrough=1 ;;
  esac
done

rc=0
for workflow in "${WORKFLOW_FILES[@]}"; do
  if [ "$passthrough" -eq 1 ]; then
    act_once "$workflow" "$@" || rc=$?
    continue
  fi

  mapfile -t LEGS < <(legs "$workflow")

  # Distinct matrix keys: with two or more the legs are a cartesian product and
  # pinning one key still leaves the others expanding in parallel, so hand it to
  # act unsplit rather than pretend otherwise.
  KEYS=$(printf '%s\n' "${LEGS[@]}" | cut -d: -f1 | sort -u | grep -c . || true)

  if [ "${#LEGS[@]}" -eq 0 ] || [ "$KEYS" -ne 1 ]; then
    [ "$KEYS" -gt 1 ] && echo "==> $workflow: matrix has $KEYS dimensions; running unsplit"
    act_once "$workflow" "$@" || rc=$?
    continue
  fi

  echo "==> $workflow: ${#LEGS[@]} matrix legs, one act run each: ${LEGS[*]}"
  for leg in "${LEGS[@]}"; do
    echo "==> leg: $leg"
    # fail-fast: false in the workflow, so run every leg and report the worst.
    act_once "$workflow" --matrix "$leg" "$@" || rc=$?
  done
done
exit "$rc"
