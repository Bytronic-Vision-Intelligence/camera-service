"""Encode/decode camera frames and build MQTT image packets."""

from __future__ import annotations

import base64
import math
import time
from time import localtime, strftime

import cv2
import numpy as np

from dependencies.image_transforms import apply_image_format

# Soft cap on each published MQTT image field (base64 chars).
DEFAULT_MAX_PACKET_BYTES = 512 * 1024


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


def build_image_topic(base_topic: str, topic_end: str | None) -> str:
    """Append ``topic_end`` to the mqtt ``image_topic`` base when set."""
    base = str(base_topic).rstrip("/")
    if topic_end is None:
        return base
    suffix = str(topic_end).strip().strip("/")
    if not suffix:
        return base
    return f"{base}/{suffix}"


def _b64_encoding(image: np.ndarray) -> tuple[str, str]:
    """Return ``(base64_text, encoding)`` for ``image``."""
    return (
        base64.b64encode(encode_image_to_bytes(image)).decode("ascii"),
        image_encoding(image),
    )


def divide_image_packets(
    image: np.ndarray,
    *,
    max_packet_bytes: int = DEFAULT_MAX_PACKET_BYTES,
) -> list[dict]:
    """Split ``image`` into horizontal strips when the encoded size is too large."""
    height, width = image.shape[:2]
    encoded, encoding = _b64_encoding(image)
    if len(encoded) <= max_packet_bytes:
        return [
            {
                "packet_number": 0,
                "packet_count": 1,
                "y0": 0,
                "y1": height,
                "x0": 0,
                "x1": width,
                "full_height": height,
                "full_width": width,
                "image": encoded,
                "encoding": encoding,
            }
        ]

    n = max(2, math.ceil(len(encoded) / max_packet_bytes))
    while n <= height:
        parts = []
        for i in range(n):
            y0, y1 = i * height // n, (i + 1) * height // n
            part_b64, part_encoding = _b64_encoding(image[y0:y1])
            if len(part_b64) > max_packet_bytes:
                parts = None
                break
            parts.append(
                {
                    "packet_number": i,
                    "packet_count": n,
                    "y0": y0,
                    "y1": y1,
                    "x0": 0,
                    "x1": width,
                    "full_height": height,
                    "full_width": width,
                    "image": part_b64,
                    "encoding": part_encoding,
                }
            )
        if parts is not None:
            return parts
        n += 1

    raise ValueError(f"Could not fit image under {max_packet_bytes} bytes per packet")


def build_packet_list(
    topic: str,
    image: np.ndarray,
    *,
    image_id: str,
    date_time: str,
    capture_id: str | None = None,
    max_packet_bytes: int = DEFAULT_MAX_PACKET_BYTES,
) -> list[dict]:
    """Build ``[{topic, payload}, ...]`` for ``client.publish_many``."""
    parts = divide_image_packets(image, max_packet_bytes=max_packet_bytes)

    def _payload(part: dict, *, split: bool) -> dict:
        payload = {
            "image": part["image"],
            "date_time": date_time,
            "image_id": image_id,
            "encoding": part["encoding"],
        }
        if capture_id:
            payload["capture_id"] = capture_id
        if split:
            payload.update(
                {
                    "packet_number": part["packet_number"],
                    "packet_count": part["packet_count"],
                    "y0": part["y0"],
                    "y1": part["y1"],
                    "x0": part["x0"],
                    "x1": part["x1"],
                    "full_height": part["full_height"],
                    "full_width": part["full_width"],
                }
            )
        return payload

    if len(parts) == 1:
        return [{"topic": topic, "payload": _payload(parts[0], split=False)}]

    group_id = f"{date_time}|{image_id}|{time.time_ns()}"
    packets = []
    for part in parts:
        payload = _payload(part, split=True)
        payload["group_id"] = group_id
        packets.append({"topic": topic, "payload": payload})
    return packets


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
