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
# additional-args may be a plain scalar or a YAML folded block (`>-`), which
# is how it is written once there is more than one flag. GitHub folds the block
# into one line; reading only the first line here gives the literal `>-` and
# silently drops every flag -- including `--paths app`, without which the
# binary builds perfectly and dies on its first import. Which is exactly what
# happened.
EXTRA_ARGS=$(awk '
  /^ *additional-args:/ {
    line = $0
    sub(/^ *additional-args: */, "", line)
    match($0, /^ */); indent = RLENGTH
    if (line == ">-" || line == ">" || line == "|" || line == "|-" || line == "") {
      while ((getline next_line) > 0) {
        if (next_line ~ /^ *$/) continue
        match(next_line, /^ */); next_indent = RLENGTH
        if (next_indent <= indent) break
        sub(/^ */, "", next_line)
        folded = folded (folded == "" ? "" : " ") next_line
      }
      print folded
    } else {
      print line
    }
    exit
  }' "$WORKFLOW")
# Strip surrounding quotes: `additional-args: ""` must mean no arguments, not a
# literal empty string, which PyInstaller would take as the script name.
EXTRA_ARGS="${EXTRA_ARGS%\"}"; EXTRA_ARGS="${EXTRA_ARGS#\"}"
EXTRA_ARGS="${EXTRA_ARGS%\'}"; EXTRA_ARGS="${EXTRA_ARGS#\'}"
: "${SCRIPT_PATH:?could not read \`scripts:\` from $WORKFLOW}"

# Which file the workflow copies in beside the binary. Read rather than
# assumed: a service whose repository is public gitignores the config.yaml the
# orchestrator writes and ships config.example.yaml instead. Hardcoding either
# name would test a file that service's release does not use.
CONFIG_SOURCE=$(sed -n 's|^ *cp \([^ ]*\) "\$output_dir/config.yaml".*|\1|p' "$WORKFLOW" | head -1)
CONFIG_SOURCE="${CONFIG_SOURCE:-config.yaml}"
: "${CONFIG_SOURCE:?could not read the config the workflow ships from $WORKFLOW}"

echo "==> script:          $SCRIPT_PATH"
echo "==> additional-args: ${EXTRA_ARGS:-(none)}"
echo "==> ships as config: $CONFIG_SOURCE"

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
    # Single-quoted: inside double quotes the backticks around git add were a
    # command substitution, so printing this message ran `git add` in the
    # developer's repository.
    echo '    A new module that main.py imports but nobody has git-added fails'
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
cp "$REPO_ROOT/$CONFIG_SOURCE" "$BUILD/config.yaml"

# Everything else the workflow copies in beside the binary, read out of the
# workflow rather than listed here. A service that ships an installer or a
# requirements file alongside its binary would otherwise be packaged one way in
# CI and another way locally, and the local run is the one that claims to check
# what a customer receives.
sed -n 's|^ *cp \([^ ]*\) "\$output_dir/\([^"]*\)".*|\1 \2|p' "$WORKFLOW" | while read -r src dst; do
  [ "$dst" = "config.yaml" ] && continue
  if [ -f "$REPO_ROOT/$src" ]; then
    cp "$REPO_ROOT/$src" "$BUILD/$dst"
    echo "==> also shipping: $dst"
  else
    echo "==> FAIL: the workflow ships $src and it is not in the tree"; exit 1
  fi
done

# The damage an artifact round-trip does: the executable bit is not preserved,
# and an empty directory is not stored.
chmod 644 "$BUILD/$(basename "$SCRIPT_PATH" .py)"
echo "==> simulated the artifact round-trip (executable bit stripped)"

# Invoked exactly as the workflow does: from the directory holding `build/`,
# with RELATIVE paths. Passing absolute ones here is what let a broken relative
# out-dir reach production - the local run resolved it and CI did not.
# The service name CI uses is github.event.repository.name; locally that is the
# directory the repository is checked out into. Hardcoding one service's name
# here -- which this script did -- makes every repository package a zip named
# after the template, so the name that ships is the one thing the local run
# never checks.
SERVICE_NAME="$(basename "$REPO_ROOT")"
( cd "$TREE/.pkg" && "$REPO_ROOT/scripts/package.sh" build upload \
    "$SERVICE_NAME" "$SCRIPT_PATH" ) >/dev/null

ZIP="$TREE/.pkg/upload/$SERVICE_NAME-linux-amd64.zip"
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

# The shipped config is used as-is unless the service cannot run under it in a
# container -- camera-service's example opens a real camera, and cv2 finds none
# here, so it would fail at startup and prove nothing about the message path.
#
# A service that needs different settings for this one check provides
# docker-local/e2e-config.yaml, plus anything it must read in
# docker-local/e2e-assets/ (copied in beside the binary, so /svc/<name> in the
# override). Both are optional, and what SHIPS is still the packaged config:
# it was checked above, and the unit suite checks it satisfies main.py.
E2E_CONFIG="$TREE/docker-local/e2e-config.yaml"
if [ -f "$E2E_CONFIG" ]; then
  echo "    config: docker-local/e2e-config.yaml (the shipped one needs hardware)"
  SOURCE_CONFIG="$E2E_CONFIG"
else
  SOURCE_CONFIG="$TREE/.pkg/x/config.yaml"
fi
[ -d "$TREE/docker-local/e2e-assets" ] && cp -R "$TREE/docker-local/e2e-assets/." "$RUN/"
sed "s/mqtt_ip: .*/mqtt_ip: $BROKER/" "$SOURCE_CONFIG" > "$RUN/config.yaml"
TOPIC=$(awk '/^ *- name:/{n=1} n&&/^ *topic:/{gsub(/^ *topic: *"?|"? *$/,""); print; exit}' "$RUN/config.yaml")
: "${TOPIC:?could not read a topic out of config.yaml}"

# A subscription may be a filter -- logging-service listens on
# project/logging/+ -- and MQTT forbids publishing to one. Substitute a literal
# segment for each wildcard so the message still matches the subscription.
PUBLISH_TOPIC=$(printf '%s' "$TOPIC" | sed -e 's|/#$|/probe|' -e 's|+|probe|g')
if [ "$PUBLISH_TOPIC" = "$TOPIC" ]; then
  echo "    topic: $TOPIC"
else
  echo "    topic: $TOPIC  (publishing to $PUBLISH_TOPIC)"
fi

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

# What counts as "it did the work" differs per service, so two signals are
# accepted and either is enough.
#
#   * the service republishes on a topic it declares as an output -- proof it
#     consumed the message and acted, without knowing anything about the
#     service; or
#   * a line in its log. E2E_MARKER overrides the default for a service that
#     reports differently.
#
# The template publishes nothing (its worker is a placeholder), so it relies on
# the marker; logging-service republishes, so it does not.
OUT_TOPIC=$(awk '/is_subscribe: *false/{found=1} /^ *- name:/{t=""} /^ *topic:/{gsub(/^ *topic: *"?|"? *$/,""); t=$0} found&&t{print t; exit}' "$RUN/config.yaml")
# A service that reports differently says so in docker-local/e2e-marker, next
# to e2e-config.yaml and e2e-assets/. In the script it would be a divergence in
# a file that is meant to be identical in every repository -- and the last time
# these drifted, one of them had been packaging every service under the
# template's name for weeks.
MARKER="${E2E_MARKER:-}"
if [ -z "$MARKER" ] && [ -f "$TREE/docker-local/e2e-marker" ]; then
  MARKER=$(head -1 "$TREE/docker-local/e2e-marker")
fi
MARKER="${MARKER:-Request received}"

if [ -n "$OUT_TOPIC" ]; then
  echo "    watching output topic: $OUT_TOPIC"
  docker exec -d "$BROKER" sh -c \
    "mosquitto_sub -h localhost -t '$OUT_TOPIC' -C 1 > /tmp/out.txt 2>&1"
  sleep 1
fi

docker exec "$BROKER" mosquitto_pub -h localhost -t "$PUBLISH_TOPIC" -m '{"command":"run"}'
received=0
for _ in $(seq 1 20); do
  if docker logs "$SERVICE" 2>&1 | grep -q "$MARKER"; then received=1; break; fi
  if [ -n "$OUT_TOPIC" ] && docker exec "$BROKER" test -s /tmp/out.txt 2>/dev/null; then
    received=1; break
  fi
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
