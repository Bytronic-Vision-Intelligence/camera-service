"""One timing line per capture: steps, total, and the clock time the frame was done."""

from __future__ import annotations

import re

import numpy as np

from dependencies.cycle_timing import CycleTiming, mark, mark_frame_done, report
from dependencies.state_machine.capture_state import CaptureState
from dependencies.state_machine.publish_state import PublishState
from fakes import FakeCamera
from test_publish_state import _Machine


class _StepCamera(FakeCamera):
    """A camera that reports its own steps, like the LJ-S driver."""
    capture_thread = None
    frame_pace = 0

    def capture_image(self, timeout_ms=None):
        self.on_step("wait ready")
        self.on_frame_done()
        self.on_step("to mm")
        return np.zeros((32, 16), np.uint16)


def test_a_cycle_reports_steps_frame_time_and_total(capsys):
    machine = type("M", (), {})()
    machine.timing = CycleTiming("depth_1", "uid-1")
    mark(machine, "delay")
    mark_frame_done(machine)
    mark(machine, "publish")
    report(machine, "published")
    line = capsys.readouterr().out.strip()
    assert re.fullmatch(r"Timing : depth_1 uid-1 received \d\d:\d\d:\d\d\.\d{3}; frame done \d\d:\d\d:\d\d\.\d{3}; "
                        r"delay \d+, capture \d+, publish \d+; total \d+ ms \(published\)", line)
    assert machine.timing is None


def test_untimed_cycles_print_nothing(capsys):
    machine = type("M", (), {})()
    mark(machine, "delay")
    report(machine, "published")
    assert capsys.readouterr().out == ""


def _capture(camera, capsys, trigger=None):
    machine = _Machine(False)
    camera.camera_config = {"camera_type": "ljs", "camera_id": "depth_1"}
    camera.trigger_config = trigger or {}
    machine.camera = camera
    machine.burst_length = 1
    machine.service_id = "depth_1"
    machine.image_outputs = [{"id": "default", "image_type": "uint16", "image_format": None, "topic_end": None}]
    machine.timing = CycleTiming("depth_1", "uid-2")
    capture = CaptureState(machine, images_captured=[])
    capture.enter()
    capture.tick()
    capture.exit()
    publish = machine.states[-1]
    assert isinstance(publish, PublishState)
    publish.enter()
    publish.tick()
    return capsys.readouterr().out


def test_a_driver_with_its_own_steps_is_timed_through_capture_and_publish(capsys):
    out = _capture(_StepCamera(), capsys)
    assert re.search(r"Timing : depth_1 uid-2 received .*; frame done .*; wait ready \d+, capture \d+, to mm \d+, "
                     r"pace sleep \d+, format \d+, encode \d+, publish \d+; total \d+ ms \(published\)", out)


def test_by_default_the_last_frame_is_paced_as_it_always_was(capsys):
    camera = FakeCamera(frame=np.zeros((32, 16), np.uint16))
    camera.capture_thread = None
    camera.frame_pace = 0
    out = _capture(camera, capsys)
    assert re.search(r"frame done .*; capture \d+, pace sleep \d+, format \d+, encode \d+, publish \d+;", out)


def test_pace_last_frame_false_publishes_the_last_frame_at_once(capsys):
    camera = FakeCamera(frame=np.zeros((32, 16), np.uint16))
    camera.capture_thread = None
    camera.frame_pace = 0
    out = _capture(camera, capsys, trigger={"pace_last_frame": False})
    assert re.search(r"frame done .*; capture \d+, format \d+, encode \d+, publish \d+;", out)
    assert "pace sleep" not in out
