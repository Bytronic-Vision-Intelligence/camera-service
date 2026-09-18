"""Pixel transforms applied to a captured frame before encode/publish.

Rotate, crop, mono conversion, channel swap, and colourmaps live here.
Encode/decode and MQTT packet shaping stay in ``image_functions``.
"""

import numpy as np
import cv2


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
