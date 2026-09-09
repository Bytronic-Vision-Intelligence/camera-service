# camera-service

Subscribes to a trigger topic, captures a frame, and publishes it to the
broker. Six camera types are supported — opencv, dummy, pylon, gige, flir and
ljs — chosen by configuration, with every vendor driver imported lazily so a
machine needs only the SDK for the camera it actually has.

## Configuration

The service runs the config it is named, and looks for none of its own:

```bash
python app/main.py --config /path/to/config.yaml
```

`--config` is required and there is no fallback. A service that found a config
beside itself would start whenever one happened to be there — a stale copy from
a previous deployment, the packaged example, or another camera's file — and
would be the wrong camera while looking healthy.

`config.example.yaml` at the repository root is the documented shape and the
copy that ships beside the binary in a release. `docs/config-examples/` holds
one file per camera type — opencv, dummy, pylon, gige, flir, ljs — as reference
material; nothing loads them.

The two topics `main.py` uses are found **by name** in `mqtt.topics`:
`trigger`, where a capture request arrives, and `image`, where the frame is
published. The camera's own settings live under `service:`, and the drivers
read them by dotted path through `app/dependencies/service_settings.py`, so
`camera.serial_number` means `service.camera.serial_number`.

### Running under service-orchestrator

[service-orchestrator](https://github.com/Bytronic-Vision-Intelligence/service-orchestrator)
launches services as `app/main.py --config <path>` with a `.venv` in each
service directory. This service already satisfies that contract.

One caveat: the orchestrator maps a config section to a directory of the same
name, so the checkout must be named `camera-service`. Instances are numbered —
`camera-service-2` shares the `camera-service/` directory and receives
`config-2.yaml`.

`/config.yaml` and `/config-*.yaml` are gitignored, because the orchestrator
writes them into the repository root at startup. This repository is public, so
what is tracked is `config.example.yaml`; the release workflow copies it into
the bundle as `config.yaml`.

## Development

```bash
python tools/setup.py                    # creates .venv/ and installs requirements
.venv/bin/python -m pytest test
```

`requirements.txt` is runtime-only — everything in it is built into the
shipped binary. Test tooling lives in `requirements-dev.txt`, which pulls in
`requirements.txt` via `-r`.

PySpin is deliberately absent from `requirements.txt`: it ships only as a
Windows wheel, so FLIR machines install it by hand. `cameras_flir.py` is
imported lazily, so every other camera type is unaffected.

OpenCV is pinned to `opencv-python-headless`. This service never calls
`imshow`/`waitKey`, and the plain build needs `libGL.so.1`, which no CI runner
or container provides.

### Running CI locally

`docker-local/` runs `.github/workflows/` on your machine through
[nektos/act](https://github.com/nektos/act). It needs Docker running and is
gitignored in some sibling repos — here it is tracked.

```bash
./docker-local/run.sh -l                        # list jobs
./docker-local/run.sh                           # what a pull request runs
./docker-local/run.sh --release                 # build, package, launch, process a message
./docker-local/run.sh --verify-release prod-N   # check a PUBLISHED release
./docker-local/run.sh --matrix os:ubuntu-latest # one matrix leg
./docker-local/run.sh --fresh                   # wipe the toolcache first
```

`--release` does not go through act: the vendor build action runs a nested
Docker step expecting its files at `/github/action/`, which act does not mount.
It reproduces the PyInstaller invocation directly, reading the entry script and
`additional-args` out of the workflow so it cannot drift from CI.

Its end-to-end stage runs the packaged binary against a real broker. The
shipped config opens a real camera, and there is none in a container, so that
stage uses `docker-local/e2e-config.yaml` — the same service in `dummy` mode,
replaying the frame in `docker-local/e2e-assets/`.

Jobs run one at a time. act shares a single `act-toolcache` volume across job
containers, so concurrent matrix legs corrupt each other's
`/opt/hostedtoolcache` — which surfaces as `Fatal Python error: Bus error`
while loading a native module. Real GitHub gives each leg its own runner.

The `windows-latest` leg runs on a Linux image, so it proves job ordering, not
Windows behaviour. And act bind-mounts the working tree rather than doing a
fresh checkout, so anything depending on what git actually committed can pass
locally and still fail on GitHub.
