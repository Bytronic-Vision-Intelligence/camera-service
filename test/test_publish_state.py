import base64
from types import SimpleNamespace

import numpy as np

from dependencies.image_functions import decode_image_from_bytes
from dependencies.state_machine.publish_state import PublishState

PLANCK = {"planck_r": 342259.0, "planck_b": 1428.0, "planck_f": 1.0, "planck_o": 406.446}
GEOMETRY = {"x_resolution": 0.3, "y_resolution": 0.547}


class FakeStateMachine:
    """Just what PublishState reads, with its publishes and next state recorded."""

    def __init__(self, camera):
        self.camera = camera
        self.archiving = {"is_archived": False}
        self.mqtt_client = SimpleNamespace(is_connected=True, published=[])
        self.mqtt_client.publish = lambda topic, packet: self.mqtt_client.published.append((topic, packet))
        self.image_outputs = [{"id": "raw", "image_type": "uint16", "topic_end": "raw"}]
        self.service_output = SimpleNamespace(topic="project/camera/thermal_1/image")
        self.next_state = None

    def change_state(self, state):
        self.next_state = state


def fake_camera(camera_type, camera_settings=None, planck_constants=None):
    camera = SimpleNamespace(
        trigger_config={},
        camera_config={"camera_type": camera_type, "camera_id": "thermal_1"},
        camera_settings=camera_settings or {},
    )
    if planck_constants is not None:
        camera.planck_constants = planck_constants
    return camera


def publish(camera, metadata=None):
    machine = FakeStateMachine(camera)
    frame = {"image": np.zeros((4, 4), dtype=np.uint16), "capture_time": "2026-10-30 15:00:00", "metadata": metadata}
    state = PublishState(machine, frame)
    state.enter()
    state.tick()
    packets = [packet for _topic, packet in machine.mqtt_client.published]
    return packets


def test_flir_planck_constants_go_into_metadata():
    packets = publish(fake_camera("flir", planck_constants=PLANCK))
    assert packets[0]["metadata"] == {"planck_constants": PLANCK}


def test_configured_planck_constants_go_into_metadata():
    packets = publish(fake_camera("dummy", camera_settings={"planck_constants": PLANCK}))
    assert packets[0]["metadata"] == {"planck_constants": PLANCK}


def test_planck_constants_stay_in_camera_details():
    packets = publish(fake_camera("flir", planck_constants=PLANCK))
    assert packets[0]["camera_details"]["planck_constants"] == PLANCK


def test_planck_constants_join_the_cameras_own_metadata():
    packets = publish(fake_camera("flir", planck_constants=PLANCK), metadata={"geometry": GEOMETRY})
    assert packets[0]["metadata"] == {"geometry": GEOMETRY, "planck_constants": PLANCK}


def test_the_cameras_metadata_is_not_changed():
    metadata = {"geometry": GEOMETRY}
    publish(fake_camera("flir", planck_constants=PLANCK), metadata=metadata)
    assert metadata == {"geometry": GEOMETRY}


def test_no_metadata_without_planck_constants_or_camera_metadata():
    packets = publish(fake_camera("dummy"))
    assert "metadata" not in packets[0]


def publish_outputs(image_outputs, image):
    machine = FakeStateMachine(fake_camera("dummy"))
    machine.image_outputs = image_outputs
    state = PublishState(machine, {"image": image, "capture_time": "2026-11-03 10:00:00"})
    state.enter()
    state.tick()
    packets = [packet for _topic, packet in machine.mqtt_client.published]
    return packets


def test_tiled_png_output_publishes_its_strips_top_to_bottom():
    image = np.random.default_rng(0).integers(0, 256, (7, 5, 3), dtype=np.uint8)

    packets = publish_outputs([{"id": "png", "image_type": "uint16", "topic_end": "png", "tiles": 3}], image)

    strips = packets[0]["image"]
    assert isinstance(strips, list) and len(strips) == 3
    out = np.vstack([decode_image_from_bytes(base64.b64decode(strip)) for strip in strips])
    assert np.array_equal(out, image)


def test_png_output_without_tiles_publishes_one_string():
    packets = publish(fake_camera("dummy"))
    assert isinstance(packets[0]["image"], str)


def test_tiles_apply_to_png_outputs_only():
    image = np.zeros((8, 8, 3), dtype=np.uint8)

    packets = publish_outputs([{"id": "jpg", "image_type": "uint8", "topic_end": None, "tiles": 6}], image)

    assert isinstance(packets[0]["image"], str)
