from numpy import frombuffer, ndarray, uint8
import cv2
from time import localtime, strftime


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
        img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
    return img


def encode_image_to_bytes(image: ndarray) -> bytes:
    """Encode the image as JPEG and return the bytes."""
    img = prepare_image_for_jpeg(image)

    success, encoded_image = cv2.imencode(".jpg", img)

    if not success:
        raise RuntimeError("Failed to encode image to JPEG format.")
    return encoded_image.tobytes()


def encode_date_time_to_bytes() -> bytes:
    """Encode the current date and time into bytes."""
    date_time = strftime("%Y-%m-%d %H:%M:%S", localtime())
    return date_time.encode("utf-8")

def decode_image_from_bytes(data: bytes) -> ndarray:
    '''decodes an image stored in bytes into an ndarray
    Args:
        data: a byte string representing the image
    Returns:
        image: an ndarray representing the image
    '''
    if not data:
        raise ValueError("Empty image bytes.")

    image = cv2.imdecode(frombuffer(data, uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("Could not decode image bytes.")
    return image


def apply_image_settings(image, image_config):
    colour_format = image_config.get("colour_format", None)
    if colour_format:
        if colour_format == "bgr_2_rgb":
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return image
        
