#!/usr/bin/env bash
#
# Thin wrapper around act. Everything after `run.sh` is passed straight through,
# so any act flag works: -l, --dryrun, -j <job>, -v, etc.

set -euo pipefail

WORKFLOWS="${WORKFLOWS_DIR:-.github/workflows}"

# checks  - what a pull request would run (the default)
# release - the release pipeline's BUILD job, which is where a release breaks
MODE="${MODE:-checks}"

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

# Which workflows to run.
#
# act filters by event, not by branch, so pointing it at the whole directory
# runs release and deployment workflows too - a plain check would try to build
# production binaries and cut a GitHub release from a developer's checkout.
# The two modes select deliberately instead:
#
#   checks  - workflows a pull request triggers. "Would my PR pass?"
#   release - workflows a pull request does NOT trigger, i.e. the release
#             pipeline. Only its build job runs; see release_jobs below.
#
# Set WORKFLOWS_DIR to override and run something specific.
ci_workflows() {
  local f has_pr
  for f in "$WORKFLOWS"/*.y*ml; do
    [ -e "$f" ] || continue
    # `on` is a YAML 1.1 boolean, so a parser may key it as "true" instead.
    if yq -e '(.on // ."true") | has("pull_request")' "$f" >/dev/null 2>&1; then
      has_pr=yes
    else
      has_pr=no
    fi
    case "$MODE:$has_pr" in
      checks:yes|release:no) printf '%s\n' "$f" ;;
    esac
  done
}

# Jobs to run in release mode, and the ones deliberately left out.
#
# A release job signs artefacts with the shared SERVICE key and publishes a
# GitHub release. Neither belongs on a developer's machine: the signing secret
# is not here, and `gh release create` would cut a real release against the
# real repository from an uncommitted working tree. The build job is the whole
# point anyway - it is where a release actually breaks, and it needs no secret.
release_jobs() {
  yq -r '.jobs | to_entries[] | select(.value.needs == null) | .key' "$1" 2>/dev/null
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

if [ "$MODE" = release ]; then
  echo "==> release mode: building only. Signing and publishing are excluded -"
  echo "    the signing key is not on this machine, and \`gh release create\`"
  echo "    would cut a real release from your working tree."
fi

rc=0
for workflow in "${WORKFLOW_FILES[@]}"; do
  # In release mode, pin to the jobs that depend on nothing - the build.
  #
  # Computed BEFORE the passthrough branch, not after: --dryrun and -l are
  # passthrough flags, and a dry run that walks the publish job prints
  # "Create GitHub release ✅ Success", which reads exactly like the thing this
  # mode promises never to do.
  JOB_ARGS=()
  if [ "$MODE" = release ]; then
    while read -r job; do
      [ -n "$job" ] && JOB_ARGS+=(-j "$job")
    done < <(release_jobs "$workflow")
    if [ "${#JOB_ARGS[@]}" -eq 0 ]; then
      echo "==> $workflow: no job without \`needs\`; skipping"
      continue
    fi
  fi

  if [ "$passthrough" -eq 1 ]; then
    act_once "$workflow" "${JOB_ARGS[@]}" "$@" || rc=$?
    continue
  fi

  mapfile -t LEGS < <(legs "$workflow")

  # Distinct matrix keys: with two or more the legs are a cartesian product and
  # pinning one key still leaves the others expanding in parallel, so hand it to
  # act unsplit rather than pretend otherwise.
  KEYS=$(printf '%s\n' "${LEGS[@]}" | cut -d: -f1 | sort -u | grep -c . || true)

  if [ "${#LEGS[@]}" -eq 0 ] || [ "$KEYS" -ne 1 ]; then
    [ "$KEYS" -gt 1 ] && echo "==> $workflow: matrix has $KEYS dimensions; running unsplit"
    act_once "$workflow" "${JOB_ARGS[@]}" "$@" || rc=$?
    continue
  fi

  echo "==> $workflow: ${#LEGS[@]} matrix legs, one act run each: ${LEGS[*]}"
  for leg in "${LEGS[@]}"; do
    echo "==> leg: $leg"
    # fail-fast: false in the workflow, so run every leg and report the worst.
    act_once "$workflow" --matrix "$leg" "${JOB_ARGS[@]}" "$@" || rc=$?
  done
done
exit "$rc"
