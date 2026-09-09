# Tests

Pytest, covering image encoding, configuration loading, and optional camera
backend smoke checks. Unit tests need no camera or broker.

```bash
.venv/bin/python -m pytest test
```

`pytest.ini` sets `pythonpath = . app`, so tests import exactly the way
`main.py` does at runtime (`from dependencies import loadConfig`).

## What is covered

- `image_functions` — mono `HxWx1` squeeze, Mono16 normalisation to 8-bit
  (truncating instead of scaling would render images black), the JPEG
  encode/decode round trip, and the guards on empty or malformed input
- `loadConfig` — `set_config_path` (the hook service-orchestrator drives via
  `main.py --config <path>`), dotted key paths such as `camera.camera_type`,
  and `get_section`'s empty-dict fallbacks
- `test_camera_backends` — for each preset in
  `test/fixtures/camera_backend_configs.yaml` (mirrors the camera blocks in
  the orchestrator `config-camera_service_test.yaml`), asserts required keys,
  then connect + capture. `dummy` always runs when the image directory exists;
  hardware types skip unless `RUN_CAMERA_HW_TESTS=1`, and still skip if the
  device is absent

`set_config_path` is process-global, so an autouse fixture resets it after
every test. Without that, one test's override leaks into the next.

Name test files `test_*.py`, lowercase. Pytest's default pattern will not
collect `Test_*.py` on a case-sensitive filesystem, which is easy to miss on
macOS and Windows.
