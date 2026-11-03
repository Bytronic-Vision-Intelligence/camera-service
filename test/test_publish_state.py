"""PublishState hands over to archiving, or straight back to idle when archiving is off."""

from __future__ import annotations

import numpy as np

from dependencies.state_machine.archive_state import ArchiveState
from dependencies.state_machine.idle_state import IdleState
from dependencies.state_machine.publish_state import PublishState
from fakes import FakeCamera, FakeMQTTClient


class _Topic:
    topic = "site/camera/colour_1/image"


class _Machine:
    def __init__(self, is_archived):
        self.camera = FakeCamera()
        self.camera.camera_config = {"camera_type": "gige", "camera_id": "colour_1"}
        self.archiving = {"is_archived": is_archived, "archive_directory": "unused"}
        self.mqtt_client = FakeMQTTClient(None)
        self.mqtt_client.is_connected = True
        self.image_outputs = [{"id": "default", "image_type": "uint8", "image_format": None, "topic_end": None}]
        self.service_output = _Topic()
        self.message_id = "uid-1"
        self.trigger_request = None
        self.states = []

    def change_state(self, state):
        self.states.append(state)


def _publish(is_archived):
    machine = _Machine(is_archived)
    state = PublishState(machine, {"image": np.zeros((4, 4, 3), np.uint8), "capture_time": "now"})
    state.enter()
    state.tick()
    return machine


def test_with_archiving_off_the_image_is_published_and_the_camera_returns_to_idle():
    machine = _publish(False)
    assert len(machine.mqtt_client.published) == 1
    assert [type(s) for s in machine.states] == [IdleState]


def test_with_archiving_on_the_image_goes_to_the_archive():
    machine = _publish(True)
    assert [type(s) for s in machine.states] == [ArchiveState]
