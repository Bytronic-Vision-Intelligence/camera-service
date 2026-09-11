# Tests

Pytest covers config resolution (including `service.*` nesting for CameraLibrary),
image helpers, camera backend presets, the main software-trigger path (fakes),
and release/packaging contracts.

```bash
python -m pytest test
```

Hardware backends are gated by `RUN_CAMERA_HW_TESTS=1`.

`pytest.ini` sets `pythonpath = . app scripts`.
