#!/usr/bin/env bash
#
# Build the service binary and smoke-test it, the way the release pipeline does.
#
#   ./build-binary.sh
#
# Why this exists separately from run.sh:
#
# act cannot execute the release pipeline's build. The espressif action runs a
# nested Docker step that expects its own files at /github/action/, which act
# does not mount, so a local act run dies with "setup_environment.sh: No such
# file or directory" long before anything is built. See readme.md.
#
# So this reproduces the build directly instead: same Python, same PyInstaller,
# same flags the workflow passes, same `--help` smoke test the action runs.
# That is the check that matters, because the failure mode is a binary that
# BUILDS perfectly and dies on its first import - unit tests cannot see it, and
# neither can anything that only inspects the workflow file.
#
# Runs against a clean copy of the TRACKED tree, so an uncommitted .venv or a
# stale dist/ cannot change the result, and nothing is written to your
# working tree.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

REPO_ROOT="$(cd .. && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/release-pipeline.yml"
PYTHON_VERSION="${PYTHON_VERSION:-3.10}"

# The flags come out of the workflow rather than being repeated here, so this
# tracks what CI actually passes instead of drifting away from it.
# The entry script is declared once, as a workflow-level `env: SCRIPT:`, and
# the build step references it as ${{ env.SCRIPT }}. Read the declaration, not
# the reference. The second form is the fallback for a repo that still names
# the script inline in `scripts:`.
SCRIPT_PATH=$(sed -n 's/^ *SCRIPT: *//p' "$WORKFLOW" | head -1 | tr -d "\"'")
[ -n "$SCRIPT_PATH" ] || SCRIPT_PATH=$(awk -F'"' '/^ *scripts:/ {print $2; exit}' "$WORKFLOW")
EXTRA_ARGS=$(awk -F': ' '/^ *additional-args:/ {sub(/^ +/,"",$2); print $2; exit}' "$WORKFLOW")
# Strip surrounding quotes: `additional-args: ""` must mean no arguments, not a
# literal empty string, which PyInstaller would take as the script name.
EXTRA_ARGS="${EXTRA_ARGS%\"}"; EXTRA_ARGS="${EXTRA_ARGS#\"}"
EXTRA_ARGS="${EXTRA_ARGS%\'}"; EXTRA_ARGS="${EXTRA_ARGS#\'}"
: "${SCRIPT_PATH:?could not read \`scripts:\` from $WORKFLOW}"

echo "==> script:          $SCRIPT_PATH"
echo "==> additional-args: ${EXTRA_ARGS:-(none)}"

TREE="$PWD/.tree"
rm -rf "$TREE"; mkdir -p "$TREE"
(cd "$REPO_ROOT" && git ls-files -z | xargs -0 tar -cf -) | tar -xf - -C "$TREE"

untracked=$(cd "$REPO_ROOT" && git ls-files --others --exclude-standard | wc -l | tr -d ' ')
[ "$untracked" -gt 0 ] && echo "==> note: $untracked untracked file(s) excluded, as CI would"

cat > "$TREE/.build.sh" <<INNER
set -eu
apt-get update -qq && apt-get install -y -qq binutils >/dev/null 2>&1
cd /work
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q pyinstaller
pyinstaller --onefile --distpath=./dist/local ${EXTRA_ARGS} \
  --add-data "./app/dependencies:./app/dependencies" ${SCRIPT_PATH}
INNER

echo "==> building in python:${PYTHON_VERSION}-slim"
# Not piped: a pipeline would report tail's exit status, and a build that died
# would look like a pass. The log is kept and shown only if something fails.
BUILD_LOG="$TREE/.build.log"
if docker run --rm -v "$TREE:/work" "python:${PYTHON_VERSION}-slim" \
     bash /work/.build.sh >"$BUILD_LOG" 2>&1; then
  grep -E "Build complete" "$BUILD_LOG" | tail -1
else
  echo "==> FAIL: PyInstaller did not complete"
  tail -15 "$BUILD_LOG"
  exit 1
fi

BINARY="$TREE/dist/local/$(basename "$SCRIPT_PATH" .py)"
[ -x "$BINARY" ] || { echo "==> FAIL: no binary produced at $BINARY"; exit 1; }
echo "==> built $(du -h "$BINARY" | cut -f1)"

# The action's own smoke test, verbatim: test_executables.sh runs the binary
# with --help and fails the build on a non-zero exit. There is no input to
# change that argument, so a service that cannot answer --help cannot ship.
echo "==> smoke test: --help"
if docker run --rm -v "$TREE:/work" "python:${PYTHON_VERSION}-slim" \
     /work/dist/local/"$(basename "$SCRIPT_PATH" .py)" --help; then
  echo "==> binary ok"
else
  echo "==> FAIL: the binary was built but does not run. This is exactly what"
  echo "    breaks a release: the build succeeds and only running it shows."
  if [ "$untracked" -gt 0 ]; then
    echo
    echo "    NOTE: $untracked untracked file(s) were excluded from this build,"
    echo "    because CI builds from a checkout and would not have them either."
    echo "    A new module that main.py imports but nobody has `git add`ed fails"
    echo "    exactly like this. Untracked:"
    (cd "$REPO_ROOT" && git ls-files --others --exclude-standard | sed 's/^/      /')
  fi
  exit 1
fi

# ---------------------------------------------------------------------------
# Package it the way the release job does, and check what a customer receives.
#
# The build job uploads an artifact and the release job downloads it, and that
# round-trip silently drops the executable bit and any empty directory. Release
# prod-1 shipped both defects having passed every job, so the simulation below
# reproduces exactly that damage before calling the REAL packaging script - not
# a copy of it, which would only ever test the copy.
# ---------------------------------------------------------------------------
BUILD="$TREE/.pkg/build/build-linux-amd64"
rm -rf "$TREE/.pkg"; mkdir -p "$BUILD"
cp "$BINARY" "$BUILD/"
cp "$REPO_ROOT/config.yaml" "$BUILD/config.yaml"

# The damage an artifact round-trip does: the executable bit is not preserved,
# and an empty directory is not stored.
chmod 644 "$BUILD/$(basename "$SCRIPT_PATH" .py)"
echo "==> simulated the artifact round-trip (executable bit stripped)"

# Invoked exactly as the workflow does: from the directory holding `build/`,
# with RELATIVE paths. Passing absolute ones here is what let a broken relative
# out-dir reach production - the local run resolved it and CI did not.
( cd "$TREE/.pkg" && "$REPO_ROOT/scripts/package.sh" build upload \
    service-template "$SCRIPT_PATH" ) >/dev/null

ZIP="$TREE/.pkg/upload/service-template-linux-amd64.zip"
[ -f "$ZIP" ] || { echo "==> FAIL: packaging produced no zip"; exit 1; }

echo "==> checking the zip as a customer receives it"
rm -rf "$TREE/.pkg/x"; mkdir -p "$TREE/.pkg/x"
( cd "$TREE/.pkg/x" && unzip -q "$ZIP" )

fail=0
BIN_IN_ZIP="$TREE/.pkg/x/$(basename "$SCRIPT_PATH" .py)"
if [ -x "$BIN_IN_ZIP" ]; then echo "    executable  ok"; else echo "    executable  NO - the customer cannot run this"; fail=1; fi
if [ -f "$TREE/.pkg/x/config.yaml" ]; then echo "    config.yaml ok"; else echo "    config.yaml MISSING"; fail=1; fi
# Asserted ABSENT, not present. Services do not write log files: they print,
# the orchestrator tees to project/logging/<service>, and logging-service is
# the only thing that writes to disk. prod-4 shipped an empty logs/ on every
# platform that nothing ever opened.
if [ -d "$TREE/.pkg/x/logs" ]; then echo "    no logs/    NO - an empty directory is shipping"; fail=1; else echo "    no logs/    ok"; fi

# The point of the whole exercise: does the unpacked thing actually launch?
if docker run --rm -v "$TREE/.pkg/x:/c" "python:${PYTHON_VERSION}-slim" \
     /c/"$(basename "$SCRIPT_PATH" .py)" --help >/dev/null 2>&1; then
  echo "    launches    ok"
else
  echo "    launches    NO"
  fail=1
fi

[ "$fail" -eq 0 ] || { echo "==> FAIL: the packaged zip is not usable as shipped"; exit 1; }

# ---------------------------------------------------------------------------
# Does the packaged binary do its JOB, not merely start?
#
# A service that connects, subscribes, logs "Subscribed to ..." and then
# silently receives nothing looks completely healthy: every log line is
# reassuring and no error is ever printed. Running it with --help does not
# notice, unpacking the zip does not notice, and the unit tests exercise the
# functions from source where this works.
#
# So the binary is run against a real broker and sent a real message.
# ---------------------------------------------------------------------------
NET=tpl-check-net
BROKER=tpl-check-broker
SERVICE=tpl-check-svc
cleanup() {
  docker rm -f "$SERVICE" "$BROKER" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> end-to-end: the binary against a real broker"
cleanup
docker network create "$NET" >/dev/null
docker run -d --rm --name "$BROKER" --network "$NET" eclipse-mosquitto:2 \
  sh -c 'printf "listener 1883\nallow_anonymous true\n" > /m.conf && mosquitto -c /m.conf' >/dev/null

RUN="$TREE/.pkg/run"
rm -rf "$RUN"; mkdir -p "$RUN"
cp "$TREE/.pkg/x/$(basename "$SCRIPT_PATH" .py)" "$RUN/"
sed "s/mqtt_ip: .*/mqtt_ip: $BROKER/" "$TREE/.pkg/x/config.yaml" > "$RUN/config.yaml"
TOPIC=$(awk '/^ *- name:/{n=1} n&&/^ *topic:/{gsub(/^ *topic: *"?|"? *$/,""); print; exit}' "$RUN/config.yaml")
: "${TOPIC:?could not read a topic out of config.yaml}"
echo "    topic: $TOPIC"

# Wait for the broker rather than sleeping a guessed amount.
for _ in $(seq 1 20); do
  docker exec "$BROKER" mosquitto_pub -h localhost -t ping -m x >/dev/null 2>&1 && break
  sleep 0.5
done

# No --platform: the build above did not pin one either, so the binary is
# whatever this host produces. Pinning amd64 here runs an arm64 binary in an
# amd64 container, which fails as "No such file or directory" and reads like
# the service crashed.
# --config is required: a service never looks for a config on its own, so it
# cannot start the wrong instance by finding a stale or example file beside it.
# A bare run exits 2, which would read here as "the binary is broken".
docker run -d --rm --name "$SERVICE" --network "$NET" \
  -v "$RUN:/svc" -w /svc "python:${PYTHON_VERSION}-slim" \
  "/svc/$(basename "$SCRIPT_PATH" .py)" --config /svc/config.yaml >/dev/null

for _ in $(seq 1 20); do
  docker logs "$SERVICE" 2>&1 | grep -q "Subscribed to" && break
  sleep 0.5
done
docker logs "$SERVICE" 2>&1 | grep -q "Subscribed to" \
  || { echo "    FAIL: the binary never subscribed"; docker logs "$SERVICE" 2>&1 | tail -5; exit 1; }
echo "    subscribed  ok"

docker exec "$BROKER" mosquitto_pub -h localhost -t "$TOPIC" -m '{"command":"run"}'
received=0
for _ in $(seq 1 20); do
  if docker logs "$SERVICE" 2>&1 | grep -q "Request received"; then received=1; break; fi
  sleep 0.5
done

if [ "$received" -eq 1 ]; then
  echo "    receives    ok"
else
  echo "    receives    NO - nothing arrived within the timeout"
  echo
  echo "    Either the service is not processing messages, or it is processing"
  echo "    them and the output is stuck in a block buffer. print() is buffered"
  echo "    when stdout is a pipe and PYTHONUNBUFFERED does NOT take effect in a"
  echo "    PyInstaller binary, so anything reporting through print() is"
  echo "    invisible here and under the orchestrator. Log instead."
  docker logs "$SERVICE" 2>&1 | tail -6 | sed 's/^/      /'
  exit 1
fi

echo "==> PASS: built, packaged, unpacked, launched, and processed a message"
