import numpy as np
import pytest

import dependencies.image_functions as image_functions
from dependencies.image_functions import (
    apply_image_format,
    build_image_topic,
    decode_image_from_bytes,
    encode_date_time_to_bytes,
    encode_image_to_bytes,
    encode_png_tiles,
    gamma_table,
    image_encoding,
    parse_image_outputs,
    _resize_img,
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

    out = decode_image_from_bytes(encode_image_to_bytes(img, np.uint8))

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
            {
                "raw": {
                    "image_type": "uint16",
                    "image_format": "Mono14",
                    "topic_end": "raw",
                    "archive": True,
                }
            },
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
    assert outputs[0]["image_type"] == "uint16"
    assert outputs[0]["archive"] is True
    assert outputs[0]["rotate"] is None
    assert outputs[0]["crop"] is None
    assert outputs[1]["image_type"] is None
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


def test_apply_image_format_colourmap_uses_fixed_norm_range():
    # Same absolute DN must map to the same colour regardless of frame min/max
    low = np.array([[6000, 7000], [8000, 9000]], dtype=np.uint16)
    high = np.array([[6000, 8500], [10000, 11000]], dtype=np.uint16)
    fmt = {"colourmap": "JET", "norm_range": [6000, 11000]}

    a = apply_image_format(low, fmt)
    b = apply_image_format(high, fmt)

    assert np.array_equal(a[0, 0], b[0, 0])
    assert not np.array_equal(a[0, 1], b[0, 1])


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


def test_resize_img_scales_both_axes():
    img = np.arange(100, dtype=np.uint8).reshape(10, 10)

    out = _resize_img(img, 0.5)

    assert out.shape == (5, 5)
    assert out.dtype == np.uint8


def test_resize_img_accepts_explicit_width_and_height():
    img = np.zeros((8, 16), dtype=np.uint16)

    out = _resize_img(img, [4, 2])

    assert out.shape == (2, 4)
    assert out.dtype == np.uint16


def test_encode_shrinks_image_to_max_size(monkeypatch):
    monkeypatch.setattr(image_functions, "MAX_IMG_SIZE", 8_000)
    img = np.random.default_rng(0).integers(0, 256, size=(320, 320, 3), dtype=np.uint8)

    encoded = encode_image_to_bytes(img, np.uint8)
    decoded = decode_image_from_bytes(encoded)

    assert len(encoded) <= 8_000
    assert decoded.shape[0] < img.shape[0]
    assert decoded.shape[1] < img.shape[1]


def test_apply_image_format_resizes_after_crop():
    img = np.arange(100, dtype=np.uint8).reshape(10, 10)

    out = apply_image_format(
        img,
        {"crop": [{"x": [0.2, 0.8]}], "resize": 0.5},
    )

    assert out.shape == (5, 3)


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


def test_encode_rejects_a_missing_encoding():
    # image_type must be set per output: no guessing from the array's dtype
    raw = np.array([[0, 16383], [100, 200]], dtype=np.uint16)

    with pytest.raises(ValueError, match="Unsupported image encoding: None"):
        encode_image_to_bytes(raw, None)


def test_encode_rejects_unknown_encoding():
    img = np.zeros((2, 2), dtype=np.uint8)

    with pytest.raises(ValueError, match="Unsupported image encoding"):
        encode_image_to_bytes(img, np.int32)


def test_encode_uint16_round_trip_as_png():
    img = apply_image_format(
        np.array([[0, 16383], [100, 200]], dtype=np.uint16),
        {"image_format": "Mono14"},
    )

    out = decode_image_from_bytes(encode_image_to_bytes(img, np.uint16))

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
    assert outputs[0]["resize"] is None
    assert outputs[0]["gamma"] is None
    assert outputs[0]["tiles"] is None


def test_gamma_table_matches_the_inference_and_seal_width_table():
    table = gamma_table(0.55)

    # the brightening the Dunbia seal model was trained on
    assert table[0] == 0 and table[255] == 255
    assert table[23] == 68
    assert table[128] == 175


def test_gamma_brightens_every_channel_alongside_the_format():
    img = np.zeros((2, 3, 3), dtype=np.uint8)
    img[..., 0] = 23   # blue in BGR, red after BGR2RGB

    out = apply_image_format(img, {"image_format": {"channel": "BGR2RGB"}, "gamma": 0.55})

    assert out.shape == img.shape
    assert (out[..., 2] == 68).all()
    assert (out[..., :2] == 0).all()


def test_no_gamma_leaves_pixels_alone():
    img = np.arange(24, dtype=np.uint8).reshape(2, 4, 3)

    assert np.array_equal(apply_image_format(img, {}), img)
    assert np.array_equal(apply_image_format(img, {"gamma": None}), img)


def test_parse_image_outputs_reads_an_optional_gamma():
    outputs = parse_image_outputs(
        [
            {"rgb": {"topic_end": None}},
            {"rgb_gamma": {"topic_end": "gamma", "gamma": 0.55}},
        ]
    )

    assert [o["gamma"] for o in outputs] == [None, 0.55]


def test_png_tiles_stack_back_to_the_exact_frame():
    img = np.random.default_rng(0).integers(0, 256, (101, 37, 3), dtype=np.uint8)

    parts = encode_png_tiles(img, 6)

    assert len(parts) == 6
    assert all(part[:4] == b"\x89PNG" for part in parts)
    out = np.vstack([decode_image_from_bytes(part) for part in parts])
    assert out.dtype == img.dtype
    assert np.array_equal(out, img)


def test_png_tiles_never_exceed_the_number_of_rows():
    img = np.zeros((3, 4), dtype=np.uint8)

    assert len(encode_png_tiles(img, 6)) == 3


def test_parse_image_outputs_reads_optional_tiles():
    outputs = parse_image_outputs(
        [
            {"rgb": {"topic_end": None}},
            {"rgb_png": {"topic_end": "png", "image_type": "uint16", "tiles": 6}},
        ]
    )

    assert [o["tiles"] for o in outputs] == [None, 6]


def test_format_output_gives_the_same_pixels_as_apply_image_format():
    raw = np.random.default_rng(1).integers(0, 256, (9, 14, 3), dtype=np.uint8)
    outputs = [
        {"rotate": -90, "gamma": 0.55, "image_format": {"channel": "BGR2RGB"}},
        {"rotate": -90, "image_format": {"channel": "BGR2RGB"}},
        {"rotate": -90, "image_format": {"channel": "BGR2RGB"}, "gamma": 0.8},
        {"gamma": 0.55, "crop": [{"x": [0.1, 0.9]}], "resize": 0.5},
        {"gamma": 0.55, "image_format": {"channel": "BGR2GRAY"}},
        {},
    ]
    formatted = {}

    for output in outputs:
        assert np.array_equal(image_functions.format_output(raw, output, formatted), apply_image_format(raw, output))
    assert len(formatted) == 3   # the three rotate + BGR2RGB outputs shared one pass; grey is not shared


def test_png_tiles_counts_strips_for_png_outputs_only():
    assert image_functions.png_tiles({"image_type": "uint16", "tiles": 8}) == 8
    assert image_functions.png_tiles({"image_type": "uint16"}) == 1
    assert image_functions.png_tiles({"image_type": "uint8", "tiles": 8}) == 1


def test_start_png_tiles_gives_the_same_strips():
    img = np.random.default_rng(2).integers(0, 256, (40, 9, 3), dtype=np.uint8)

    assert image_functions.start_png_tiles(img, 4).result(timeout=10) == encode_png_tiles(img, 4)


def test_base64_text_and_packet_bytes_match_the_standard_library():
    import base64
    import json
    data = bytes(range(256)) * 50
    packet = {"image": [image_functions.base64_text(data)] * 2, "date_time": "2026-11-04 16:37:54.570802",
              "camera_details": {"exposure_time_us": 1500, "gain": 1.5, "camera_ip": None}}

    assert packet["image"][0] == base64.b64encode(data).decode("ascii")
    out = image_functions.packet_bytes(packet)
    assert json.loads(out) == packet if isinstance(out, bytes) else out is packet


def test_packet_bytes_leaves_what_orjson_cannot_serialise_to_mqtt_client():
    packet = {"image": "QUJD", "camera_details": {"gain": np.float32(1.5)}}

    assert image_functions.packet_bytes(packet) is packet


def test_without_pybase64_and_orjson_packets_are_built_as_before(monkeypatch):
    import base64
    import importlib
    import sys
    monkeypatch.setitem(sys.modules, "pybase64", None)   # import fails as if not installed
    monkeypatch.setitem(sys.modules, "orjson", None)
    try:
        plain = importlib.reload(image_functions)
        packet = {"image": plain.base64_text(b"abc"), "date_time": "x"}
        assert packet["image"] == base64.b64encode(b"abc").decode("ascii")
        assert plain.packet_bytes(packet) is packet   # mqtt_client serialises the dict, as before
    finally:
        monkeypatch.undo()
        importlib.reload(image_functions)


def test_parse_image_outputs_reads_payload_format_json_by_default():
    outputs = parse_image_outputs(
        [
            {"rgb": {"topic_end": None}},
            {"rgb_png": {"topic_end": "png", "payload_format": "Binary"}},
        ]
    )

    assert [o["payload_format"] for o in outputs] == ["json", "binary"]
    assert resolve_image_outputs({})[0]["payload_format"] == "json"


def test_parse_image_outputs_refuses_an_unknown_payload_format():
    with pytest.raises(ValueError, match="payload_format"):
        parse_image_outputs([{"rgb": {"payload_format": "bytes"}}])
