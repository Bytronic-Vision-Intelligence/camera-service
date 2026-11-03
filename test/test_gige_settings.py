"""GigE camera settings written at start-up and per capture: white balance and per-colour exposure."""

from __future__ import annotations

import pytest

pytest.importorskip("harvesters")

from dependencies.CameraLibrary.cameras_gige import GigeCamera  # noqa: E402


class _Node:
    def __init__(self, nodes: "_NodeMap", name: str):
        self._nodes, self._name = nodes, name

    @property
    def value(self):
        return self._nodes.values.get(self._name)

    @value.setter
    def value(self, value):
        self._nodes.write(self._name, value)


class _NodeMap:
    '''records every write in order; ExposureTime keeps one value per selected channel'''

    def __init__(self, **values):
        self.values = {"ExposureTimeSelector": "Common", **values}
        self.writes = []

    def write(self, name, value):
        if name == "ExposureTime":
            self.writes.append((name, self.values["ExposureTimeSelector"], value))
        else:
            self.writes.append((name, value))
        self.values[name] = value

    def __getattr__(self, name):
        return _Node(self, name)


class _Device:
    def __init__(self, nm):
        self.node_map = nm


class _Cam:
    def __init__(self, nm):
        self.remote_device = _Device(nm)


def _camera(settings: dict) -> GigeCamera:
    camera = GigeCamera.__new__(GigeCamera)
    camera.camera_settings = settings
    return camera


def test_common_exposure_without_ratios():
    nm = _NodeMap()
    _camera({"exposure_time_us": 10000})._apply_camera_settings(_Cam(nm))
    assert ("ExposureTime", "Common", 10000.0) in nm.writes
    assert not any(w[0] in ("ExposureTimeMode", "BalanceWhiteAuto") for w in nm.writes)


def test_white_balance_and_per_colour_exposure_are_set_at_start():
    nm = _NodeMap()
    settings = {"exposure_time_us": 15000, "white_balance": "Off",
                "exposure_ratios": {"red": 1.136, "blue": 1.621}}
    _camera(settings)._apply_camera_settings(_Cam(nm))
    names = [w[0] for w in nm.writes]
    assert ("BalanceWhiteAuto", "Off") in nm.writes
    assert ("ExposureTimeMode", "Individual") in nm.writes
    # the mode is switched before any exposure is written
    assert names.index("ExposureTimeMode") < names.index("ExposureTime")
    exposures = {w[1]: w[2] for w in nm.writes if w[0] == "ExposureTime"}
    assert exposures == {"Green": 15000.0, "Red": pytest.approx(17040.0), "Blue": pytest.approx(24315.0)}


def test_per_capture_exposure_keeps_the_colour_ratios():
    nm = _NodeMap()
    camera = _camera({"exposure_ratios": {"red": 1.136, "blue": 1.621}})
    camera._set_exposure(_Cam(nm), 20000)        # the HMI's 20 ms, as capture_state passes it
    exposures = {w[1]: w[2] for w in nm.writes if w[0] == "ExposureTime"}
    assert exposures == {"Green": 20000.0, "Red": pytest.approx(22720.0), "Blue": pytest.approx(32420.0)}


def test_white_balance_failure_is_reported():
    class _Refusing(_NodeMap):
        def write(self, name, value):
            if name == "BalanceWhiteAuto":
                raise RuntimeError("not writable")
            super().write(name, value)

    with pytest.raises(RuntimeError, match="BalanceWhiteAuto=Off"):
        _camera({"white_balance": "Off"})._apply_camera_settings(_Cam(_Refusing()))
