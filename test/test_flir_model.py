"""Unit tests for FLIR camera_model profiles (a70 vs ax5)."""

from __future__ import annotations

import pytest

from dependencies.CameraLibrary.cameras_flir import flir_model_profile


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


def test_ax5_profile_uses_mono14_and_mask():
    profile = flir_model_profile("ax5")
    assert profile["default_pixel_format"] == "Mono14"
    assert profile["set_cmos_bit_depth"] is True
    assert profile["default_cmos_bit_depth"] == "bit14bit"
    assert profile["mask_mono16_msbs"] is True
    assert profile["pixel_format_fallbacks"][0] == "Mono14"
    assert profile["temperature_linear_ir_format"] is None


def test_flir_model_profile_defaults_blank_to_ax5():
    assert flir_model_profile("")["default_pixel_format"] == "Mono14"
    assert flir_model_profile(None)["default_pixel_format"] == "Mono14"
