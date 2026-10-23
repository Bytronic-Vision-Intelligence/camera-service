"""Unit tests for FLIR camera_model profiles (a70 vs ax5)."""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

# PySpin ships only as a Windows Spinnaker wheel and is not installed in CI.
if importlib.util.find_spec("PySpin") is None:
    sys.modules["PySpin"] = types.ModuleType("PySpin")

from dependencies.CameraLibrary import cameras_flir  # noqa: E402
from dependencies.CameraLibrary.cameras_flir import FlirCamera, flir_model_profile  # noqa: E402


def _record_enum_writes(monkeypatch):
    calls = []

    def _enum(_nodemap, name, entry):
        calls.append((name, entry))
        return True

    def _bool(*_args):
        calls.append(("bool", _args[1:]))
        return True

    monkeypatch.setattr(cameras_flir, "_set_enum_node", _enum)
    monkeypatch.setattr(cameras_flir, "_set_bool_node", _bool)
    return calls


def test_flir_model_profile_rejects_unknown():
    with pytest.raises(ValueError, match="Unsupported FLIR camera_model"):
        flir_model_profile("boson")


def test_a70_profile_skips_ax5_nodes():
    profile = flir_model_profile("a70")
    assert profile["default_pixel_format"] == "Mono16"
    assert profile["set_cmos_bit_depth"] is False
    assert profile["mask_mono16_msbs"] is False
    assert "Mono14" not in profile["pixel_format_fallbacks"]
    assert profile["temperature_linear_ir_format"] == "TemperatureLinear10mK"
    assert "default_temperature_linear_resolution" not in profile


def test_ax5_profile_uses_mono14_and_mask():
    profile = flir_model_profile("ax5")
    assert profile["default_pixel_format"] == "Mono14"
    assert profile["set_cmos_bit_depth"] is True
    assert profile["default_cmos_bit_depth"] == "bit14bit"
    assert profile["mask_mono16_msbs"] is True
    assert profile["pixel_format_fallbacks"][0] == "Mono14"
    assert "temperature_linear_ir_format" not in profile
    assert profile["default_temperature_linear_resolution"] == "High"


def test_flir_model_profile_defaults_blank_to_ax5():
    assert flir_model_profile("")["default_pixel_format"] == "Mono14"
    assert flir_model_profile(None)["default_pixel_format"] == "Mono14"


def test_ax5_configure_does_not_write_a70_nodes(monkeypatch):
    calls = _record_enum_writes(monkeypatch)
    camera = FlirCamera()
    camera.camera_settings = {
        "camera_model": "ax5",
        "temperature_linear_mode": True,
        "temperature_linear_resolution": "High",
    }

    camera._configure_raw_pixel_format(object())

    names = [name for name, _entry in calls]
    assert "IRFormat" not in names
    assert ("CMOSBitDepth", "bit14bit") in calls
    assert ("PixelFormat", "Mono14") in calls
    assert ("TemperatureLinearMode", "On") in calls
    assert ("TemperatureLinearResolution", "High") in calls
    assert camera._mask_mono16_msbs is False


def test_a70_configure_does_not_write_ax5_nodes(monkeypatch):
    calls = _record_enum_writes(monkeypatch)
    camera = FlirCamera()
    camera.camera_settings = {
        "camera_model": "A70",
        "temperature_linear_mode": "true",
        "pixel_format": "Mono16",
    }

    camera._configure_raw_pixel_format(object())

    names = [name for name, _entry in calls]
    assert "CMOSBitDepth" not in names
    assert "TemperatureLinearMode" not in names
    assert "TemperatureLinearResolution" not in names
    assert ("PixelFormat", "Mono16") in calls
    assert ("IRFormat", "TemperatureLinear10mK") in calls
    assert camera._mask_mono16_msbs is False
    assert camera._flir_model == "a70"
