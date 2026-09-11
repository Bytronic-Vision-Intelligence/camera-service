# Local CI runner

Runs `.github/workflows/` on this machine via [nektos/act](https://github.com/nektos/act),
so a workflow can be checked before opening a PR.

This folder is committed; only `logs/` and `artifacts/` are gitignored. Copy it
into another service repo as-is — nothing in it names a particular repository. `run.sh` derives `PROJECT_NAME` from the
parent directory name, which is what the compose project and image are named
after.

## Requirements

Docker running. Nothing else — act and the docker CLI are installed inside the
image, and the host daemon is driven through the mounted socket, so job
containers are siblings of the runner, not children.

## Usage

```bash
./run.sh -l          # list jobs
./run.sh --dryrun    # execution order, nothing executed
./run.sh             # checks: what a pull request would run
./run.sh --release   # build the binary and smoke-test it
./run.sh --all       # checks, then the binary build
./run.sh --verify-release [tag]   # check a PUBLISHED release
./run.sh -j build    # one job
./run.sh pull_request
./run.sh --fresh     # wipe act's toolcache volume first
```

### Checks, and the binary build

`./run.sh` runs the workflows a pull request triggers, through act.

`./run.sh --release` does **not** use act. It calls `build-binary.sh`, which
takes a release all the way through:

1. builds with PyInstaller, reading the entry script and `additional-args` out
   of the workflow so it cannot drift from what CI passes
2. runs the binary with `--help`, exactly as the build action's smoke test does
3. **simulates the artifact round-trip** — strips the executable bit, the way
   `actions/upload-artifact` does
4. calls `scripts/package.sh`, the *same script CI runs*, not a copy
5. unzips the result and checks it as a customer receives it: binary
   executable, `config.yaml` and `logs/` present — then **launches it**

Steps 3 to 5 are what release `prod-1` needed and did not have. The packaging
logic lives in a script precisely so this can execute the real thing: what it
repairs is damage done between two CI jobs, and a reimplementation in a test
would only ever test the reimplementation.

**Why not act for the release build.** The pipeline calls
`espressif/python-binary-action`, which on Linux runs a nested Docker step
expecting its own files at `/github/action/`. act does not mount them, so the
run dies with `setup_environment.sh: No such file or directory` before anything
is built. `./run.sh --release-workflow` still drives the workflow through act if
you want to check the steps *around* the action; it will fail at the action
itself, and that is act's limitation rather than a problem with the pipeline.

When act does run a release workflow, only jobs with no `needs` are executed —
the build. A release job signs artefacts with the shared SERVICE key and runs
`gh release create`; neither belongs on a developer's machine, and the second
would cut a real release from an uncommitted working tree. That restriction is
applied before act sees `--dryrun` or `-l`, so even a dry run cannot walk the
publish job and print `Create GitHub release ✅ Success`.

This catches the two failure modes nothing else can. A binary missing an import
still *builds*, at full size, with no warning — it dies the first time it is
run. And a binary that builds and runs perfectly can still be shipped in a zip
the customer cannot execute. Unit tests see neither; nor does anything that only
reads the workflow file. Between them they took down both of the first two
`prod` releases.

### Verifying a published release

`./run.sh --verify-release` downloads a release and checks it the way a
customer receives it: the detached signature verifies under the key committed
in `scripts/sign.py`, the zip matches `SHA256SUMS`, the binary inside is
**executable**, and `config.yaml` and `logs/` are present.

This is the only check that sees what actually ships. The build tests a binary
before it is uploaded, and `build-binary.sh` never goes through an artifact at
all — so neither can see anything lost in the round-trip between the build and
packaging jobs. Release `prod-1` passed every job and shipped a binary the
customer could not execute, with `logs/` missing, on all three platforms.

Both builds run against a clean copy of the **tracked** tree, never the live
working directory. The act mount is read-write, so a build running `uv venv` in
the repository root collides with the `.venv` you run pytest in — a failure CI
never has, because `actions/checkout` starts from nothing — and anything the
build writes lands in your working tree. Uncommitted edits to tracked files
*are* tested; untracked files are not, and the run says how many it skipped.

On an Apple Silicon machine the build runs natively as arm64 even though the
workflow names `linux-amd64`, so what it proves is the *logic* — that imports
resolve and the binary answers `--help` — and not the amd64 artefact. Same
caveat as the windows-latest leg: ordering and behaviour are real, the target
is not. Only GitHub's runners produce the artefact you ship.

Any other flag is passed straight through to act.

`--fresh` matters when you change `requirements.txt`: act keeps
`/opt/hostedtoolcache` in a named volume and pip installs into it, so a run can
otherwise pass on packages a previous run left behind.

Runs are tee'd to `logs/`, with `logs/latest.log` symlinked to the most recent.
The container is `--rm`, so without that the output would die with it. Artifacts
land in `artifacts/`.

## What this does not prove

The `windows-latest` leg is mapped onto a Linux image so the job graph resolves
and ordering is visible. It says nothing about Windows behaviour — only a real
GitHub runner does.

act bind-mounts the working tree rather than doing a fresh `git checkout`. Any
defect that lives in the difference between your working tree and what git
actually has — a file renamed only by case, an untracked file the CI would
never see — will pass here and fail on GitHub.

## A note on `--fresh`

`act-toolcache` is a single Docker volume shared by **every** repo using this
runner, not one per project. That is why a stale cache from another service can
surface here.

`--fresh` wipes it, but the first run afterwards can fail spuriously:
`setup-python` resolves a Python build for the runner it thinks it is on
(e.g. `python-3.10.21-linux-24.04-x64.tar.gz`) which may not match the
`catthehacker/ubuntu` image, giving:

```
./setup.sh: line 55: ./python: No such file or directory   (exit 127)
```

Run it a second time without `--fresh`. Once one leg has populated the cache,
subsequent legs find it there and skip the download. Reach for `--fresh` only
when you have changed `requirements.txt` and suspect a stale package, and
expect to run twice.
