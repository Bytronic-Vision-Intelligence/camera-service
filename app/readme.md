# app

The camera worker: subscribe to a trigger, capture a frame, publish it.

## Entry point

`app/main.py`. It runs the configuration it is given and nothing else:

```bash
python app/main.py --config /path/to/config.yaml
```

`--config` is required and there is no fallback. A service that found a config
on its own would start whenever one happened to be beside it — a stale copy
from a previous deployment, the packaged example, or another camera's file —
and would be the wrong camera while looking perfectly healthy.

## What it reads

`../config.example.yaml` documents the shape; `../docs/config-examples/` has
one file per camera type. In outline:

- `mqtt.mqtt_ip`, `mqtt.mqtt_port` — the broker.
- `mqtt.topics` — looked up **by name**. `trigger` is where a capture request
  arrives (subscribed only when the camera is software-triggered); `image` is
  where the encoded frame is published.
- `service.camera` — `camera_type`, `camera_id`, `capture_timeout_ms`, and
  whatever the chosen driver needs.
- `service.trigger.trigger_type` — `software`/`internal` waits for an MQTT
  message; `hardware`/`external` means the camera delivers frames itself.
- `service.archiving` — whether frames are also written to disk.
- `logging.level`.

`dependencies/loadConfig.py` is copied verbatim from `service-template` and is
identical in every Bytronic service, so nothing camera-specific belongs in it.
The drivers read their own settings through `dependencies/service_settings.py`
instead, by dotted path beneath `service:` — `camera.serial_number` there means
`service.camera.serial_number` in the file.

## Camera types

`opencv` (the local webcam), `dummy` (replays images from a directory),
`pylon`, `gige`, `flir`, `ljs`. Everything but `opencv` is imported lazily:
each needs a vendor SDK that is not installed on a machine running a different
camera, and a module-level import would stop the service starting at all.

An unsupported `camera_type` raises `ValueError`.

## Logging

Through `logging`, never `print()`. Under service-orchestrator a service's
stdout is a pipe, so `print()` output sits in a block buffer until several KB
accumulate and is lost outright if the service crashes — exactly when it is
wanted. `PYTHONUNBUFFERED` does not help: PyInstaller's bootloader configures
the interpreter itself and ignores it.

`dependencies/logging_setup.py` emits the one format logging-service parses. A
line it cannot parse is recorded as INFO whatever severity it claims, so a
camera loss would be filed as routine.
