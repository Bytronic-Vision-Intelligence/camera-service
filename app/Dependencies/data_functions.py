from numpy import ndarray, uint8
from cv2 import imencode, normalize, NORM_MINMAX, CV_8U
from datetime import datetime


def prepare_image_for_jpeg(image: ndarray) -> ndarray:
    """Return an 8-bit image suitable for JPEG (keeps mono HxW raw layout)."""
    if image is None:
        raise ValueError("Input image is None.")
    if not isinstance(image, ndarray):
        raise ValueError("Input image must be a numpy array.")

    img = image
    if img.ndim == 3 and img.shape[2] == 1:
        img = img[:, :, 0]

    if img.dtype != uint8:
        # Mono16 / float etc. → uint8 without expanding to BGR
        img = normalize(img, None, 0, 255, NORM_MINMAX, dtype=CV_8U)
    return img


def encode_image_to_bytes(image: ndarray) -> bytes:
    """Encode the image as JPEG and return the bytes."""
    img = prepare_image_for_jpeg(image)

    success, encoded_image = imencode(".jpg", img)

    if not success:
        raise RuntimeError("Failed to encode image to JPEG format.")
    return encoded_image.tobytes()


def encode_date_time_to_bytes() -> bytes:
    """Encode the capture time as exactly 23 UTF-8 bytes.

    The width is a wire contract, not a formatting choice: inference slices the
    last 23 bytes off the packet and parses them with "%Y-%m-%d %H:%M:%S.%f".
    A 19-byte stamp leaves 4 JPEG bytes inside that slice, and the 0xFF end-of-
    image marker is not valid UTF-8 — so the decode raised, the timestamp stayed
    empty, and every frame was discarded with "bad timestamp ''".

    The milliseconds are load-bearing too: without them there is no resolution
    to measure the trigger-to-capture delay with.
    """
    date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return date_time.encode("utf-8")
