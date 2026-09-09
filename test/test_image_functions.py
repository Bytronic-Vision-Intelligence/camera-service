import numpy as np
import pytest

from dependencies.image_functions import (
    decode_image_from_bytes,
    encode_date_time_to_bytes,
    encode_image_to_bytes,
    prepare_image_for_jpeg,
)


def test_prepare_image_squeezes_a_single_channel_axis():
    # mono cameras hand back HxWx1; JPEG wants HxW
    img = np.zeros((4, 6, 1), dtype=np.uint8)

    assert prepare_image_for_jpeg(img).shape == (4, 6)


def test_prepare_image_normalises_mono16_to_uint8():
    # Mono16 must be scaled, not truncated, or the image goes black
    img = np.array([[0, 4096], [8192, 65535]], dtype=np.uint16)

    out = prepare_image_for_jpeg(img)

    assert out.dtype == np.uint8
    assert out.min() == 0 and out.max() == 255


def test_prepare_image_leaves_uint8_untouched():
    img = np.arange(16, dtype=np.uint8).reshape(4, 4)

    assert np.array_equal(prepare_image_for_jpeg(img), img)


def test_prepare_image_rejects_none_and_non_arrays():
    with pytest.raises(ValueError):
        prepare_image_for_jpeg(None)
    with pytest.raises(ValueError):
        prepare_image_for_jpeg([[1, 2], [3, 4]])


def test_encode_decode_round_trip_preserves_shape():
    img = np.arange(64, dtype=np.uint8).reshape(8, 8)

    out = decode_image_from_bytes(encode_image_to_bytes(img))

    assert out.shape == img.shape
    assert out.dtype == np.uint8


def test_decode_image_rejects_empty_and_garbage():
    # imdecode returns None rather than raising, so the guard has to catch it
    with pytest.raises(ValueError):
        decode_image_from_bytes(b"")
    with pytest.raises(ValueError):
        decode_image_from_bytes(b"not an image at all")


def test_encode_date_time_is_utf8_and_parseable():
    from datetime import datetime

    raw = encode_date_time_to_bytes()

    assert isinstance(raw, bytes)
    datetime.strptime(raw.decode("utf-8"), "%Y-%m-%d %H:%M:%S")
