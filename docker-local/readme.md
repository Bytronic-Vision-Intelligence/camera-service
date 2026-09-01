# Local CI runner

Runs `.github/workflows/` on this machine via [nektos/act](https://github.com/nektos/act),
so a workflow can be checked before opening a PR.

This folder is gitignored. Copy it into another service repo as-is — nothing
in it names a particular repository. `run.sh` derives `PROJECT_NAME` from the
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
./run.sh             # the push event, end to end
./run.sh -j build    # one job
./run.sh pull_request
./run.sh --fresh     # wipe act's toolcache volume first
```

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
