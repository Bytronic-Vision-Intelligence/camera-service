import base64
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from dependencies.image_functions import (
    BINARY_PACKET_MAGIC,
    apply_image_format,
    decode_image_from_bytes,
    encode_image_to_bytes,
)
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


def as_dict(packet):
    """A published packet as the receiver sees it: JSON bytes (orjson installed) or the dict.
    A binary packet stays bytes (see unpack)."""
    if isinstance(packet, bytes) and packet[:4] == BINARY_PACKET_MAGIC:
        return packet
    out = packet if isinstance(packet, dict) else json.loads(packet)
    return out


def publish(camera, metadata=None):
    machine = FakeStateMachine(camera)
    frame = {"image": np.zeros((4, 4), dtype=np.uint16), "capture_time": "2026-10-30 15:00:00", "metadata": metadata}
    state = PublishState(machine, frame)
    state.enter()
    state.tick()
    packets = [as_dict(packet) for _topic, packet in machine.mqtt_client.published]
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


def publish_outputs(image_outputs, image, topics=None):
    machine = FakeStateMachine(fake_camera("dummy"))
    machine.image_outputs = image_outputs
    state = PublishState(machine, {"image": image, "capture_time": "2026-11-03 10:00:00"})
    state.enter()
    state.tick()
    packets = [as_dict(packet) for _topic, packet in machine.mqtt_client.published]
    if topics is not None:
        topics.extend(topic for topic, _packet in machine.mqtt_client.published)
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


def unpack(payload: bytes) -> dict:
    """What a receiver does with a binary packet: the JSON header, the image bytes cut by length."""
    assert payload[:4] == BINARY_PACKET_MAGIC
    size = int.from_bytes(payload[4:8], "big")
    packet = json.loads(payload[8:8 + size])
    start = 8 + size
    sizes = packet["image"]
    if isinstance(sizes, list):
        parts = []
        for n in sizes:
            parts.append(payload[start:start + n])
            start += n
        packet["image"] = parts
    else:
        packet["image"] = payload[start:start + sizes]
        start += sizes
    assert start == len(payload)
    return packet


def test_binary_tiled_png_carries_the_strips_as_bytes():
    image = np.random.default_rng(0).integers(0, 256, (7, 5, 3), dtype=np.uint8)

    packets = publish_outputs([{"id": "png", "image_type": "uint16", "topic_end": "png", "tiles": 3,
                                "payload_format": "binary"}], image)

    assert isinstance(packets[0], bytes)
    packet = unpack(packets[0])
    assert len(packet["image"]) == 3
    out = np.vstack([decode_image_from_bytes(strip) for strip in packet["image"]])
    assert np.array_equal(out, image)
    assert packet["image_id"] == "png" and packet["date_time"] == "2026-11-03 10:00:00"


def test_binary_packet_has_the_same_fields_as_the_json_one():
    image = np.random.default_rng(1).integers(0, 256, (6, 4, 3), dtype=np.uint8)
    outputs = [{"id": "jpg", "image_type": "uint8", "topic_end": None}]

    as_json = publish_outputs(outputs, image)[0]
    as_binary = unpack(publish_outputs([dict(outputs[0], payload_format="binary")], image)[0])

    assert as_binary["image"] == base64.b64decode(as_json["image"])
    assert {k: v for k, v in as_binary.items() if k != "image"} == {k: v for k, v in as_json.items() if k != "image"}


LINE4_OUTPUTS = [   # config_dunbia_seal_width.yaml: the pack model's JPEG, seal width's tiled PNG, the UI copy
    {"id": "rgb_gamma", "image_type": "uint8", "rotate": -90, "image_format": {"channel": "BGR2RGB"},
     "gamma": 0.55, "topic_end": "gamma"},
    {"id": "rgb_png", "image_type": "uint16", "rotate": -90, "image_format": {"channel": "BGR2RGB"},
     "tiles": 3, "topic_end": "png"},
    {"id": "rgb", "image_type": "uint8", "rotate": -90, "image_format": {"channel": "BGR2RGB"}, "topic_end": None},
]


def test_shared_formatting_publishes_the_same_images_in_the_same_order():
    raw = np.random.default_rng(5).integers(0, 256, (12, 20, 3), dtype=np.uint8)
    topics = []

    packets = publish_outputs(LINE4_OUTPUTS, raw, topics)

    assert topics == ["project/camera/thermal_1/image/gamma", "project/camera/thermal_1/image/png",
                      "project/camera/thermal_1/image"]
    for output, packet in zip(LINE4_OUTPUTS, packets):
        expected = apply_image_format(raw, output)   # each output formatted on its own, as before
        if output["id"] == "rgb_png":
            strips = [decode_image_from_bytes(base64.b64decode(s)) for s in packet["image"]]
            assert np.array_equal(np.vstack(strips), expected)
        else:   # the same JPEG bytes as encoding that output's own formatted image
            assert base64.b64decode(packet["image"]) == encode_image_to_bytes(expected, np.uint8)


def test_line4_outputs_with_a_binary_png_publish_the_same_pixels():
    raw = np.random.default_rng(9).integers(0, 256, (12, 20, 3), dtype=np.uint8)
    outputs = [dict(o, payload_format="binary") if o["id"] == "rgb_png" else o for o in LINE4_OUTPUTS]

    packets = publish_outputs(outputs, raw)

    assert isinstance(packets[0], dict) and isinstance(packets[2], dict)   # the JPEGs stay JSON
    png = unpack(packets[1])
    assert np.array_equal(np.vstack([decode_image_from_bytes(s) for s in png["image"]]),
                          apply_image_format(raw, outputs[1]))


def test_a_grey_output_is_not_shared_with_a_colour_one():
    raw = np.random.default_rng(6).integers(0, 256, (12, 20, 3), dtype=np.uint8)
    outputs = [{"id": "grey", "image_type": "uint8", "gamma": 0.55, "image_format": {"channel": "BGR2GRAY"},
                "topic_end": "grey"},
               {"id": "colour", "image_type": "uint8", "gamma": 0.55, "topic_end": None}]

    packets = publish_outputs(outputs, raw)

    for output, packet in zip(outputs, packets):
        assert base64.b64decode(packet["image"]) == encode_image_to_bytes(apply_image_format(raw, output), np.uint8)
    grey = cv2.imdecode(np.frombuffer(base64.b64decode(packets[0]["image"]), np.uint8), cv2.IMREAD_UNCHANGED)
    assert grey.ndim == 2


def test_binary_packet_is_not_utf8_text():
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    payload = publish_outputs([{"id": "jpg", "image_type": "uint8", "payload_format": "binary"}], image)[0]
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")
