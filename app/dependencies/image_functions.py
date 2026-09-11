import numpy as np
import cv2
from time import localtime, strftime


def prepare_image_for_jpeg(image: np.ndarray) -> np.ndarray:
    """Return an 8-bit image suitable for JPEG (keeps mono HxW raw layout)."""
    if image is None:
        raise ValueError("Input image is None.")
    if not isinstance(image, np.ndarray):
        raise ValueError("Input image must be a numpy array.")

    img = image
    if img.ndim == 3 and img.shape[2] == 1:
        img = img[:, :, 0]

    if img.dtype != np.uint8:
        # Mono16 / float etc. → uint8 without expanding to BGR
        img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
    return img


def encode_image_to_bytes(image: np.ndarray) -> bytes:
    """Encode the image as JPEG (8-bit) or PNG (uint16)."""
    if image.dtype == np.uint16:
        success, encoded_image = cv2.imencode(".png", image)
        if not success:
            raise RuntimeError("Failed to encode image to PNG format.")
        return encoded_image.tobytes()

    img = prepare_image_for_jpeg(image)
    success, encoded_image = cv2.imencode(".jpg", img)
    if not success:
        raise RuntimeError("Failed to encode image to JPEG format.")
    return encoded_image.tobytes()


def image_encoding(image: np.ndarray) -> str:
    """Return the on-wire encoding used by :func:`encode_image_to_bytes`."""
    if isinstance(image, np.ndarray) and image.dtype == np.uint16:
        return "png"
    return "jpeg"


def encode_date_time_to_bytes() -> bytes:
    """Encode the current date and time into bytes."""
    date_time = strftime("%Y-%m-%d %H:%M:%S", localtime())
    return date_time.encode("utf-8")


def decode_image_from_bytes(data: bytes) -> np.ndarray:
    """Decode an image stored in bytes into an ndarray."""
    if not data:
        raise ValueError("Empty image bytes.")

    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("Could not decode image bytes.")
    return image


def _rotation_code(degrees) -> int:
    """Map degrees to a ``cv2.ROTATE_*`` code (90 / -90 / 180 / 270)."""
    angle = int(degrees)
    if angle == 180:
        return cv2.ROTATE_180
    if angle == 90:
        return cv2.ROTATE_90_CLOCKWISE
    if angle == 270 or angle == -90:
        return cv2.ROTATE_90_COUNTERCLOCKWISE
    raise ValueError(f"Unsupported rotate: {degrees!r} (use 90, -90, 180, or 270)")


def _apply_crop(image: np.ndarray, crop) -> np.ndarray:
    """Crop with fractional ``x`` / ``y`` ranges, e.g. ``[{x: [0.15, 0.85]}]``."""
    h, w = image.shape[:2]
    x0, x1, y0, y1 = 0, w, 0, h
    for item in crop:
        axis, (start, end) = next(iter(item.items()))
        if axis == "x":
            x0, x1 = int(start * w), int(end * w)
        elif axis == "y":
            y0, y1 = int(start * h), int(end * h)
    return image[y0:y1, x0:x1]


def _color_conversion_code(name: str) -> int:
    attr = f"COLOR_{str(name).strip()}"
    if hasattr(cv2, attr):
        return getattr(cv2, attr)
    raise ValueError(f"Unsupported channel conversion: {name!r}")


def _colormap_code(name: str) -> int:
    attr = str(name).strip()
    if not attr.startswith("COLORMAP_"):
        attr = f"COLORMAP_{attr}"
    if hasattr(cv2, attr):
        return getattr(cv2, attr)
    raise ValueError(f"Unsupported colourmap: {name!r}")


def _parse_norm_range(norm_range) -> tuple[float, float] | None:
    """Validate optional ``norm_range: [min, max]`` for fixed colormap scaling."""
    if norm_range is None:
        return None
    if not isinstance(norm_range, (list, tuple)) or len(norm_range) != 2:
        raise ValueError(f"norm_range must be [min, max], got {norm_range!r}")
    vmin, vmax = float(norm_range[0]), float(norm_range[1])
    if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        raise ValueError(f"norm_range max must be > min, got {norm_range!r}")
    return vmin, vmax


def _to_grayscale_uint8(
    image: np.ndarray,
    norm_range: tuple[float, float] | list | None = None,
) -> np.ndarray:
    """Collapse to single-channel uint8.

    When ``norm_range`` is set, scale that fixed DN window to 0–255 (values
    outside are clipped). Otherwise use per-frame min/max normalisation.
    """
    img = image
    if img.ndim == 3:
        if img.shape[2] == 1:
            img = img[:, :, 0]
        else:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if img.dtype != np.uint8:
        parsed = _parse_norm_range(norm_range)
        if parsed is not None:
            vmin, vmax = parsed
            scaled = (img.astype(np.float32) - vmin) * (255.0 / (vmax - vmin))
            img = np.clip(scaled, 0, 255).astype(np.uint8)
        else:
            img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
    return img


def _bit_mask(bits: int) -> int:
    if bits < 1 or bits > 16:
        raise ValueError(f"uint16 bit depth must be 1-16 (got {bits})")
    return (1 << bits) - 1


def _to_uint16(image: np.ndarray, bits: int | None = None) -> np.ndarray:
    """Return a 2D uint16 mono image, optionally masked to ``bits``."""
    img = image
    if img.ndim == 3:
        if img.shape[2] == 1:
            img = img[:, :, 0]
        else:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    max_value = _bit_mask(bits) if bits is not None else 65535

    if img.dtype == np.uint16:
        out = img
    elif img.dtype == np.uint8:
        out = img.astype(np.uint16)
    elif img.dtype.kind == "f":
        out = img.clip(0, max_value).round().astype(np.uint16)
    else:
        out = img.clip(0, max_value).astype(np.uint16)

    if bits is not None:
        out = np.bitwise_and(out, np.uint16(max_value))
    return out


def _apply_mono_format(image: np.ndarray, name: str) -> np.ndarray:
    """Convert to MonoN / uint16 / uint8."""
    lower = name.lower()

    if lower == "uint8":
        return _to_grayscale_uint8(image)
    if lower == "uint16":
        return _to_uint16(image)
    if lower.startswith("mono"):
        depth = int(name[4:])
        return _to_uint16(image, depth)

    raise ValueError(f"Unsupported mono image_format: {name!r}")


def apply_image_format(image: np.ndarray, output_settings) -> np.ndarray:
    """Apply rotate, crop, then ``image_format``. ``None`` format keeps raw pixels."""
    if not isinstance(output_settings, dict):
        raise ValueError("output_settings must be a mapping")

    if output_settings.get("rotate") is not None:
        image = cv2.rotate(image, _rotation_code(output_settings["rotate"]))

    if output_settings.get("crop") is not None:
        image = _apply_crop(image, output_settings["crop"])

    image_format = output_settings.get("image_format")
    if image_format is None:
        return image

    if isinstance(image_format, str):
        return _apply_mono_format(image, image_format.strip())

    if not isinstance(image_format, dict):
        raise ValueError("image_format must be a string, mapping, or null")

    if "channel" in image_format:
        if image.ndim < 3 or image.shape[2] < 3:
            raise ValueError(
                f"channel {image_format['channel']!r} requires a 3-channel image"
            )
        return cv2.cvtColor(image, _color_conversion_code(image_format["channel"]))

    if "colourmap" in image_format:
        return cv2.applyColorMap(
            _to_grayscale_uint8(image, norm_range=image_format.get("norm_range")),
            _colormap_code(image_format["colourmap"]),
        )

    raise ValueError(f"Unsupported image_format: {image_format!r}")


def build_image_topic(base_topic: str, topic_end: str | None) -> str:
    """Append ``topic_end`` to the mqtt ``image_topic`` base when set."""
    base = str(base_topic).rstrip("/")
    if topic_end is None:
        return base
    suffix = str(topic_end).strip().strip("/")
    if not suffix:
        return base
    return f"{base}/{suffix}"


def parse_image_outputs(images_config) -> list[dict]:
    """Parse ``images`` config into normalised output descriptors."""
    if not isinstance(images_config, list) or not images_config:
        raise ValueError("images must be a non-empty list")

    outputs: list[dict] = []
    for entry in images_config:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(
                f"Each images entry must be a single-key dict: {entry!r}"
            )
        image_id, spec = next(iter(entry.items()))
        if not isinstance(spec, dict):
            raise ValueError(f"Image spec for {image_id!r} must be a mapping")

        outputs.append(
            {
                "id": str(image_id),
                "image_format": spec.get("image_format"),
                "rotate": spec.get("rotate"),
                "crop": spec.get("crop"),
                "topic_end": spec.get("topic_end"),
                "archive": bool(spec.get("archive", False)),
            }
        )
    return outputs


def resolve_image_outputs(config: dict) -> list[dict]:
    """Return configured image outputs, or a single passthrough default."""
    if "images" in config:
        return parse_image_outputs(config["images"])
    return [
        {
            "id": "default",
            "image_format": None,
            "rotate": None,
            "crop": None,
            "topic_end": None,
            "archive": True,
        }
    ]
