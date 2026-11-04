"""Capture speed-ups: exposure written only when it changes, exact fast LJ-S decoding,
exact fast float -> uint16 rounding, and triggering at once when the head was seen ready."""

from __future__ import annotations

import ctypes

import numpy as np
import pytest

import dependencies.CameraLibrary.cameras_ljs as ljs
from dependencies.CameraLibrary.cameras_ljs import LJSCamera, decode_height_mm
from dependencies.image_functions import _to_uint16
from dependencies.state_machine.capture_state import apply_exposure


class _ExposureCamera:
    cam = object()

    def __init__(self):
        self.writes = []

    def _set_exposure(self, cam, value):
        self.writes.append(value)


def test_exposure_is_written_once_and_skipped_while_unchanged():
    camera = _ExposureCamera()
    assert apply_exposure(camera, [15, 17.5], 0) is True
    assert apply_exposure(camera, [15, 17.5], 0) is False      # same value: no write
    assert apply_exposure(camera, [15, 17.5], 1) is True       # next burst frame differs
    assert apply_exposure(camera, [15, 17.5], 5) is False      # list repeats its last value
    assert apply_exposure(camera, None, 0) is False
    assert camera.writes == [15000, 17500]


def _ljs(interpolate_y=4, pitch=1020):
    camera = LJSCamera()
    camera.interpolate_y = interpolate_y
    camera._pitch_z = pitch
    camera._mm_lookup = decode_height_mm(np.arange(65536, dtype=np.uint16), pitch)
    return camera


def test_lookup_decode_matches_the_formula_for_every_raw_value():
    camera = _ljs(interpolate_y=1)
    raw = np.arange(65536, dtype=np.uint16).reshape(256, 256)
    assert np.array_equal(camera._decode(raw), decode_height_mm(raw, 1020), equal_nan=True)


def test_lookup_decode_repeats_rows_like_before():
    rng = np.random.default_rng(5)
    raw = rng.integers(0, 65536, (800, 320), dtype=np.uint16)
    raw[rng.random(raw.shape) < 0.4] = 0                       # no-data points
    camera = _ljs(interpolate_y=4)
    expected = np.repeat(decode_height_mm(raw, 1020), 4, axis=0)
    got = camera._decode(raw)
    assert got.dtype == np.float32 and got.shape == (3200, 320)
    assert np.array_equal(got, expected, equal_nan=True)


def test_float_to_uint16_rounds_exactly_as_before():
    rng = np.random.default_rng(6)
    image = rng.uniform(-5, 300, (1200, 500)).astype(np.float32)
    image[rng.random(image.shape) < 0.3] = np.nan
    image[0, :4] = [0.5, 1.5, 2.5, 70000.0]                    # half-to-even and clipping
    with np.errstate(invalid="ignore"):
        expected = image.clip(0, 65535).round().astype(np.uint16)
    assert np.array_equal(_to_uint16(image), expected)


class _FakeLJSwrap:
    """Stands in for the DLL: records triggers, refuses the first if told to."""

    def __init__(self, refuse_first=False, ready=True):
        self.triggers = 0
        self.status_calls = 0
        self.refuse_first = refuse_first
        self.ready = ready

    def LJS8IF_Trigger(self, device_id):
        self.triggers += 1
        return 0x8001 if (self.refuse_first and self.triggers == 1) else 0

    def LJS8IF_GetAttentionStatus(self, device_id, status):
        self.status_calls += 1
        status.value = ljs._TRG_READY if self.ready else 0
        return 0


def _connected(monkeypatch, fake):
    monkeypatch.setattr(ljs, "LJSwrap", fake)
    camera = _ljs(interpolate_y=1)
    camera._connected = True
    camera._raw_queue.put(np.full((4, 4), 32768 + 1000, np.uint16))
    return camera


def test_a_head_seen_ready_while_idle_is_triggered_without_polling(monkeypatch):
    fake = _FakeLJSwrap()
    camera = _connected(monkeypatch, fake)
    assert camera.prepare_for_trigger() is True
    calls_before = fake.status_calls
    image = camera.capture_image(timeout_ms=1000)
    assert fake.status_calls == calls_before and fake.triggers == 1
    assert image.shape == (4, 4)


def test_a_refused_trigger_falls_back_to_polling_and_retries(monkeypatch):
    fake = _FakeLJSwrap(refuse_first=True)
    camera = _connected(monkeypatch, fake)
    camera.prepare_for_trigger()
    camera.capture_image(timeout_ms=1000)
    assert fake.triggers == 2


def test_without_the_idle_check_the_head_is_polled_first(monkeypatch):
    fake = _FakeLJSwrap()
    camera = _connected(monkeypatch, fake)
    camera.capture_image(timeout_ms=1000)
    assert fake.status_calls >= 1 and fake.triggers == 1
