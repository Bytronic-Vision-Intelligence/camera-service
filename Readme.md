# Camera service

One-process-per-camera MQTT capture worker built on the Bytronic service
template. Supports six backends (dummy, opencv, GigE, FLIR, Pylon, LJS), dual
event sources (MQTT trigger vs hardware/continuous frame thread), and
multi-variant image publish (raw / colourmap / RGB).

## Prerequisites

- Python 3.10 or newer
- An MQTT broker at `localhost:1883` (typical)
- Backend SDKs as needed (Baumer CTI, Spinnaker, pylon, Keyence LJS DLL)

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

```bash
python app/main.py --config ./config.yaml
```

Site examples (local; gitignored):

```bash
python app/main.py --config ./config-dunbia_seal.yaml
python app/main.py --config ./config.example.yaml
```

```bash
python -m pytest test
```

## What it does

1. Connects the camera backend from `service.camera.camera_type`
2. **Software / single:** subscribes to the `trigger` topic; payload must
   contain `"trigger"`; then `capture_image`
3. **Software / continuous:** MQTT on/off arms a timed capture stream (no
   repeated trigger messages)
4. **Hardware / single:** camera line/edge IO via `wait_for_frame`
5. **Hardware / continuous:** camera free-run stream via `wait_for_frame`
6. For each `service.images` entry: format → optional archive → JPEG/PNG encode
   → publish `{image, date_time, image_id, encoding}` on
   `{image topic}/{topic_end}`. An entry's `encoding` (`png` | `jpeg` | `bmp`) picks the
   wire format; unset, 16-bit frames go as PNG and 8-bit frames as JPEG. Set
   `png` on an 8-bit output that analysis measures from, and keep a JPEG output
   for display. `bmp` is lossless too and much faster to encode and decode, but
   uncompressed: a 2448x2048 colour frame is ~20 MB on the wire, so the broker's
   `max_packet_size` must allow it.

Orchestrator runs multiple instances (`camera-service`, `-2`, `-3`) each with
its own config file.

## Configuration

**`--config PATH` is required.**

| Topic `name` | Role |
|---|---|
| `trigger` | subscribe + trigger (MQTT modes) |
| `image` | publish base; `topic_end` appends `/raw`, `/colourmap`, … |

Domain knobs live under `service:`: `camera`, `trigger`, `camera_settings`,
`lights`, `images`, `archiving`. CameraLibrary still reads them via
`loadConfig.get_section` / dotted `return_config_value`, which resolve under
`service` first.

Logging is stdout only (`logging.level`).

## Releasing

Push to `prod` runs the signed release pipeline. Keep `SERVICE_SIGNING_KEY` and
`scripts/sign.py` `EXPECTED_PUBLIC_KEY` aligned.

- `requirements.txt` — runtime
- `requirements-dev.txt` — tests / CI
- `requirements-signing.txt` — release job only
- FLIR: install Spinnaker wheel by hand on Windows

## Project layout

- `app/main.py` — dual-path loop + publish
- `app/dependencies/CameraLibrary/` — vendor backends
- `app/dependencies/image_functions.py` / `archive_functions.py`
- `config.yaml` — tracked shape

License: see `docs/LICENSE`.
