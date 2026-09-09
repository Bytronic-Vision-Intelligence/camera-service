"""Backend smoke tests for each camera_type preset.

Presets live in ``test/fixtures/camera_backend_configs.yaml`` (same values as
the commented blocks in config-library/config-camera_service_test.yaml).

``dummy`` always runs when the image directory exists. Hardware backends skip
unless ``RUN_CAMERA_HW_TESTS=1``, and still skip if the device is missing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

from dependencies import loadConfig
from main import set_camera_class

CAMERA_SERVICE_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CONFIG = Path(__file__).resolve().parent / "fixtures" / "camera_backend_configs.yaml"

COMMON_REQUIRED = {"camera_type", "camera_id", "capture_timeout"}

TYPE_REQUIRED = {
    "dummy": {"dummy_location", "file_type"},
    "opencv": set(),
    "gige": {"serial_number", "cti_path"},
    "flir": {"serial_number"},
    "pylon": {"serial_number"},
    "ljs": {"ljs"},
}

HW_TYPES = {"opencv", "gige", "flir", "pylon", "ljs"}


@pytest.fixture(autouse=True)
def restore_config_path():
    yield
    loadConfig.set_config_path(None)


def _load_fixture() -> dict:
    data = yaml.safe_load(FIXTURE_CONFIG.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        pytest.fail(f"{FIXTURE_CONFIG} root must be a mapping")
    return data


def _camera_types() -> dict[str, dict]:
    types = _load_fixture().get("camera_types")
    if not isinstance(types, dict) or not types:
        pytest.fail("fixtures camera_types must be a non-empty mapping")
    return types


def _service_shell(fixture: dict) -> dict:
    shell = {
        key: value
        for key, value in fixture.items()
        if key != "camera_types"
    }
    for required in ("mqtt", "trigger", "archiving"):
        if required not in shell:
            pytest.fail(f"fixture missing {required} section")
    return shell


def _write_config(tmp_path: Path, shell: dict, camera: dict) -> Path:
    payload = {**shell, "camera": dict(camera)}
    dummy = payload["camera"].get("dummy_location")
    if isinstance(dummy, str) and dummy and not Path(dummy).is_absolute():
        payload["camera"]["dummy_location"] = str(
            (CAMERA_SERVICE_ROOT / dummy).resolve()
        )

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _assert_frame(image, camera_type: str) -> None:
    assert image is not None, f"{camera_type}: capture returned None"
    assert isinstance(image, np.ndarray), f"{camera_type}: expected ndarray, got {type(image)}"
    assert image.size > 0, f"{camera_type}: empty frame"
    assert image.ndim in (2, 3), f"{camera_type}: unexpected ndim={image.ndim}"
    assert image.shape[0] > 0 and image.shape[1] > 0, f"{camera_type}: bad shape {image.shape}"


@pytest.mark.parametrize("camera_type", sorted(TYPE_REQUIRED))
def test_camera_type_preset_declares_required_keys(camera_type):
    types = _camera_types()
    assert camera_type in types, f"missing camera_types.{camera_type} in fixture"

    preset = types[camera_type]
    assert isinstance(preset, dict)
    assert preset.get("camera_type") == camera_type

    missing = (COMMON_REQUIRED | TYPE_REQUIRED[camera_type]) - set(preset)
    assert not missing, f"{camera_type} preset missing keys: {sorted(missing)}"


@pytest.mark.parametrize("camera_type", sorted(TYPE_REQUIRED))
def test_camera_type_connect_and_capture(camera_type, tmp_path):
    import os

    fixture = _load_fixture()
    preset = fixture["camera_types"][camera_type]
    config_path = _write_config(tmp_path, _service_shell(fixture), preset)
    loadConfig.set_config_path(config_path)

    if camera_type in HW_TYPES and os.environ.get("RUN_CAMERA_HW_TESTS") != "1":
        pytest.skip(
            f"set RUN_CAMERA_HW_TESTS=1 to exercise {camera_type} hardware"
        )

    if camera_type == "dummy":
        location = Path(loadConfig.return_config_value("camera.dummy_location"))
        if not location.is_dir():
            pytest.skip(f"dummy_location missing: {location}")

    camera = None
    try:
        try:
            camera = set_camera_class(camera_type, dict(preset))
        except Exception as exc:
            if camera_type in HW_TYPES:
                pytest.skip(f"{camera_type} not available: {exc}")
            raise

        timeout = int(preset.get("capture_timeout") or 1000)
        image = camera.capture_image(timeout_ms=timeout)
        _assert_frame(image, camera_type)
    finally:
        if camera is not None:
            try:
                camera.disconnect_camera(getattr(camera, "cam", None))
            except Exception:
                pass
