"""Images encoded as horizontal strips in parallel, joined into one wire text."""

from __future__ import annotations

import base64

import cv2
import numpy as np
import pytest

from dependencies.image_functions import encode_image_to_bytes, parse_image_outputs
from dependencies.image_strips import STRIP_SEPARATOR, encode_strips, strip_bounds
from dependencies.state_machine.publish_state import PublishState
from test_publish_state import _Machine


def _decode(text: str) -> np.ndarray:
    """What the receiving services do: decode each strip and stack them."""
    parts = [cv2.imdecode(np.frombuffer(base64.b64decode(p), np.uint8), cv2.IMREAD_UNCHANGED)
             for p in text.split(STRIP_SEPARATOR)]
    return np.vstack(parts)


def _depth(h=210, w=150):
    rng = np.random.default_rng(1)
    return rng.integers(0, 4000, (h, w), dtype=np.uint16)


def test_strip_bounds_cover_every_row_once_on_16_row_boundaries():
    bounds = strip_bounds(2056, 4)
    assert bounds[0][0] == 0 and bounds[-1][1] == 2056
    assert all(a[1] == b[0] for a, b in zip(bounds, bounds[1:]))
    assert all(top % 16 == 0 for top, _ in bounds)
    assert len(bounds) == 4


def test_a_short_image_gets_no_more_strips_than_it_has_16_row_blocks():
    assert strip_bounds(40, 8) == [(0, 16), (16, 40)]
    assert strip_bounds(10, 4) == [(0, 10)]


def test_one_strip_is_exactly_the_old_single_image_format():
    image = _depth()
    encode = lambda a: encode_image_to_bytes(a, np.uint16)
    assert encode_strips(image, 1, encode) == base64.b64encode(encode(image)).decode("ascii")


@pytest.mark.parametrize("strips", [2, 4, 8])
def test_png_strips_rebuild_the_image_exactly(strips):
    image = _depth()
    text = encode_strips(image, strips, lambda a: encode_image_to_bytes(a, np.uint16))
    assert text.count(STRIP_SEPARATOR) == len(strip_bounds(image.shape[0], strips)) - 1
    assert np.array_equal(_decode(text), image)


def test_jpeg_strips_rebuild_a_colour_image_of_the_same_size():
    image = np.full((100, 64, 3), 128, np.uint8)
    text = encode_strips(image, 4, lambda a: encode_image_to_bytes(a, np.uint8))
    assert _decode(text).shape == image.shape


def test_strips_default_to_one_and_are_checked():
    outputs = parse_image_outputs([{"a": {"image_type": "uint8"}}, {"b": {"image_type": "uint8", "strips": 4}}])
    assert [o["strips"] for o in outputs] == [1, 4]
    for bad in (0, 33, 2.5, "4", True):
        with pytest.raises(ValueError):
            parse_image_outputs([{"a": {"image_type": "uint8", "strips": bad}}])


def test_publish_sends_strips_when_the_output_asks_for_them():
    machine = _Machine(False)
    machine.image_outputs = [{"id": "default", "image_type": "uint16", "image_format": None, "topic_end": None, "strips": 4}]
    image = _depth(400, 120)
    state = PublishState(machine, {"image": image, "capture_time": "now"})
    state.enter()
    state.tick()
    (_, packet), = machine.mqtt_client.published
    assert packet["image"].count(STRIP_SEPARATOR) == 3
    assert np.array_equal(_decode(packet["image"]), image)


def test_8_bit_output_is_scaled_over_the_whole_frame_not_per_strip():
    machine = _Machine(False)
    machine.image_outputs = [{"id": "default", "image_type": "uint8", "image_format": None, "topic_end": None, "strips": 2}]
    image = np.zeros((64, 8), np.uint16)
    image[:32] = 100          # top strip dark, bottom strip bright
    image[32:] = 1000
    state = PublishState(machine, {"image": image, "capture_time": "now"})
    state.enter()
    state.tick()
    (_, packet), = machine.mqtt_client.published
    decoded = _decode(packet["image"])
    assert decoded[:32].max() < 10 and decoded[32:].min() > 245
