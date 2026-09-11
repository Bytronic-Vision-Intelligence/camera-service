# Application

`main.py` is the entrypoint. It runs the config named by `--config`, connects
the camera backend, then either:

- starts MQTT subscribers on `is_subscribe` topics (software trigger), or
- starts a `wait_for_frame` thread (hardware / continuous FLIR/dummy)

and publishes formatted image packets on the `image` topic base.

## Domain modules

| Path | Purpose |
|---|---|
| `CameraLibrary/` | opencv, dummy, gige, flir, pylon, ljs backends |
| `image_functions.py` | format, encode, multi-output topics |
| `archive_functions.py` | optional disk archive |
| `loadConfig.py` | required `--config` only; domain knobs injected into cameras |
| `logging_setup.py` | stdout logging for the orchestrator |

## Running

```bash
python main.py --config ../config.yaml
```

See the repository [README](../Readme.md).
