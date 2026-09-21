"""DummyCamera must preserve 16-bit thermal PNG depth."""

from pathlib import Path

import cv2
import numpy as np

from dependencies.CameraLibrary.cameras_dummy import DummyCamera


def test_dummy_capture_preserves_uint16(tmp_path: Path):
    frame = np.array([[27553, 31254], [28000, 30000]], dtype=np.uint16)
    path = tmp_path / "thermal.png"
    assert cv2.imwrite(str(path), frame)

    cam = DummyCamera()
    cam.camera_config = {
        "dummy_location": str(tmp_path),
        "file_type": ".png",
    }
    cam.connect_to_camera()
    out = cam.capture_image()

    assert out is not None
    assert out.dtype == np.uint16
    assert out.ndim == 2
    np.testing.assert_array_equal(out, frame)
