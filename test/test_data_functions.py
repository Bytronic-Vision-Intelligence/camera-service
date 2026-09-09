import numpy as np
import pytest

from dependencies.image_functions import (
    apply_image_format,
    build_image_topic,
    decode_image_from_bytes,
    encode_date_time_to_bytes,
    encode_image_to_bytes,
    image_encoding,
    parse_image_outputs,
    prepare_image_for_jpeg,
    resolve_image_outputs,
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


def test_parse_image_outputs_reads_ids_and_flags():
    outputs = parse_image_outputs(
        [
            {"raw": {"image_format": "Mono14", "topic_end": "raw", "archive": True}},
            {
                "colourmap": {
                    "image_format": {"colourmap": "JET"},
                    "topic_end": "colourmap",
                    "archive": False,
                }
            },
            {
                "rgb": {
                    "rotate": -90,
                    "crop": [{"x": [0.15, 0.85]}],
                    "image_format": {"channel": "BGR2RGB"},
                    "topic_end": None,
                    "archive": True,
                }
            },
        ]
    )

    assert outputs[0]["id"] == "raw"
    assert outputs[0]["archive"] is True
    assert outputs[0]["rotate"] is None
    assert outputs[0]["crop"] is None
    assert outputs[1]["topic_end"] == "colourmap"
    assert outputs[2]["rotate"] == -90
    assert outputs[2]["crop"] == [{"x": [0.15, 0.85]}]


def test_build_image_topic_appends_topic_end():
    assert build_image_topic("project/camera/colour/image", "raw") == (
        "project/camera/colour/image/raw"
    )
    assert build_image_topic("project/camera/colour/image", None) == (
        "project/camera/colour/image"
    )


def test_apply_image_format_channel_and_colourmap():
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    img[:, :, 0] = 255

    rgb = apply_image_format(img, {"image_format": {"channel": "BGR2RGB"}})
    assert rgb[0, 0, 2] == 255

    thermal = np.array([[0, 32768], [65535, 16384]], dtype=np.uint16)
    coloured = apply_image_format(thermal, {"image_format": {"colourmap": "JET"}})
    assert coloured.shape == (2, 2, 3)


def test_apply_image_format_mono14_masks_to_bit_depth():
    img = np.array([[0, 4096], [8192, 65535]], dtype=np.uint16)

    out = apply_image_format(img, {"image_format": "Mono14"})

    assert out.shape == (2, 2)
    assert out.dtype == np.uint16
    assert out[1, 1] == 16383


def test_apply_image_format_uint8_normalises_mono16():
    img = np.array([[0, 32768], [65535, 0]], dtype=np.uint16)

    out = apply_image_format(img, {"image_format": "uint8"})

    assert out.dtype == np.uint8
    assert out.max() == 255


def test_apply_image_format_uint16_keeps_full_range():
    img = np.array([[0, 65535]], dtype=np.uint16)

    out = apply_image_format(img, {"image_format": "uint16"})

    assert out.dtype == np.uint16
    assert out[0, 1] == 65535


def test_apply_image_format_rotate_and_crop():
    # Distinct pixel so rotate/crop orientation is easy to check.
    img = np.arange(100, dtype=np.uint8).reshape(10, 10)

    rotated = apply_image_format(img, {"rotate": -90})
    assert rotated.shape == (10, 10)
    assert rotated[0, 0] == img[0, 9]

    cropped = apply_image_format(img, {"crop": [{"x": [0.2, 0.8]}]})
    assert cropped.shape == (10, 6)
    assert np.array_equal(cropped, img[:, 2:8])

    both = apply_image_format(
        img,
        {"rotate": 90, "crop": [{"y": [0.0, 0.5]}]},
    )
    assert both.shape == (5, 10)


def test_encode_uint16_round_trip_as_png():
    img = apply_image_format(
        np.array([[0, 16383], [100, 200]], dtype=np.uint16),
        {"image_format": "Mono14"},
    )

    out = decode_image_from_bytes(encode_image_to_bytes(img))

    assert out.dtype == np.uint16
    assert np.array_equal(out, img)


def test_image_encoding_reports_png_for_uint16():
    img = apply_image_format(np.zeros((4, 4), dtype=np.uint16), {"image_format": "Mono14"})

    assert image_encoding(img) == "png"


def test_resolve_image_outputs_defaults_without_images_key():
    outputs = resolve_image_outputs({})

    assert len(outputs) == 1
    assert outputs[0]["id"] == "default"
    assert outputs[0]["rotate"] is None
    assert outputs[0]["crop"] is None
