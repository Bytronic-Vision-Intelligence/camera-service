import os
import logging
import threading
import datetime
import uuid

import numpy as np
from cv2 import imwrite

# Float height maps (mm) → 16-bit PNG. 0 = invalid/NaN; else:
#   stored = round(mm * RAW_PNG_MM_SCALE) + 32768
# Recover with decode_raw_height_png() / (img.astype(float) - 32768) / RAW_PNG_MM_SCALE
RAW_PNG_MM_SCALE = 100.0  # 0.01 mm resolution
BIT_SCALE_15 = 32768.0

# Truncated uuid4. 4 hex chars is enough to split two saves that share
# camera, image type, sku, and the same second. The timestamp is already
# in the filename.
ARCHIVE_ID_HEX_LEN = 8


def new_capture_id() -> str:
    """Short id shared by every camera that handled the same trigger."""
    return uuid.uuid4().hex[:ARCHIVE_ID_HEX_LEN]


def _filename_token(value, default: str) -> str:
    """Keep a filename field free of path separators and the ``__`` delimiter."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return default
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            cleaned.append(ch)
        else:
            cleaned.append("_")
    token = "".join(cleaned).strip("_")
    while "__" in token:
        token = token.replace("__", "_")
    return token or default


# Locked filename order when ``default_order`` is true. A missing value is left out.
FILENAME_CATEGORIES = (
    "datetime",
    "uuid",
    "camera_id",
    "camera_type",
    "image_type",
    "sku_id",
    "verdict",
)


def filename_categories(archive_params: dict | None) -> list[str]:
    """``FILENAME_CATEGORIES`` when ``default_order`` is on, otherwise the config list."""
    params = archive_params or {}
    default_order = params.get("default_order", True)
    if isinstance(default_order, str):
        default_order = default_order.strip().lower() not in {"0", "false", "no", "off"}
    if default_order:
        return list(FILENAME_CATEGORIES)

    names = []
    raw = params.get("filename_categories")
    items = raw.split(",") if isinstance(raw, str) else (raw or [])
    for item in items:
        if isinstance(item, dict):
            continue
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in FILENAME_CATEGORIES:
            logging.warning("Unknown archive filename category %r", name)
            continue
        names.append(name)
    return names or list(FILENAME_CATEGORIES)


def build_archive_filename(
    categories: list,
    values: dict | None = None,
    ext: str = "png",
    when: datetime.datetime | None = None,
    archive_id: str | None = None,
) -> str:
    """Join ``categories`` with ``__``, skipping any field that has no value.

    ``datetime`` is ``YYYYMMDD_hhmmss``. ``uuid`` is a 4-hex-char id when
    ``values`` does not already supply one.
    """
    when = when or datetime.datetime.now()
    values = values or {}
    parts = []
    for category in categories:
        name = str(category).strip().lower()
        if name not in FILENAME_CATEGORIES:
            continue
        token = _category_token(name, values, when, archive_id)
        if token:
            parts.append(token)
    if not parts:
        parts.append(archive_id or uuid.uuid4().hex[:ARCHIVE_ID_HEX_LEN])
    suffix = _filename_token(ext, "png").lstrip(".")
    return f"{'__'.join(parts)}.{suffix}"


def _category_token(name: str, values: dict, when: datetime.datetime, archive_id: str | None) -> str:
    if name == "uuid":
        short_id = archive_id or values.get("uuid") or uuid.uuid4().hex[:ARCHIVE_ID_HEX_LEN]
        return _filename_token(short_id, "0")
    if name == "datetime":
        stamp = values.get("datetime", when)
        if isinstance(stamp, datetime.datetime):
            return stamp.strftime("%Y%m%d_%H%M%S")
        text = str(stamp).strip() if stamp is not None else ""
        return text or when.strftime("%Y%m%d_%H%M%S")
    raw = values.get(name)
    if raw is None or not str(raw).strip():
        return ""
    return _filename_token(raw, "")


def _archive_save_dir(directory, archive_params: dict, camera_id=None) -> str | None:
    """Build ``archive_directory/YYYYMMDD[_hh]/camera_id``. Returns None on bad input.

    ``archive_freq`` ``daily`` uses ``YYYYMMDD``. ``hourly`` uses ``YYYYMMDD_hh``.
    """
    if directory == "" or directory is None:
        logging.error("no directory specified")
        return None
    base_directory = os.fspath(directory)
    try:
        os.makedirs(base_directory, exist_ok=True)
    except Exception as e:
        logging.error(f"failed to create base archive directory {directory}: {e}")
        return None

    subfolder = None
    archive_freq = archive_params.get("archive_freq", None)
    if archive_freq:
        save_timestamp = datetime.datetime.now()
        if archive_freq == "daily":
            subfolder = save_timestamp.strftime("%Y%m%d")
        elif archive_freq == "hourly":
            subfolder = save_timestamp.strftime("%Y%m%d_%H")

    path_parts = [base_directory]
    if subfolder:
        path_parts.append(subfolder)
    if camera_id is not None:
        path_parts.append(_filename_token(camera_id, "0"))
    save_directory = os.path.join(*path_parts)
    os.makedirs(save_directory, exist_ok=True)
    return save_directory


def prepare_raw_png(image: np.ndarray) -> np.ndarray:
    """Convert a capture array to a PNG-writable integer image that keeps values.

    - uint8 / uint16: written as-is (16-bit PNG for Mono14/FLIR counts).
    - float (e.g. LJS mm): NaN/inf → 0; finite mm encoded as
      ``uint16(round(mm * 100) + 32768)`` (0.01 mm steps). Use
      :func:`decode_raw_height_png` to recover millimetres.
    """
    arr = np.asarray(image)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]

    if arr.dtype == np.uint8 or arr.dtype == np.uint16:
        return arr

    if np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(np.uint16)
        return np.clip(arr, info.min, info.max).astype(np.uint16)

    if np.issubdtype(arr.dtype, np.floating):
        out = np.zeros(arr.shape, dtype=np.uint16)
        valid = np.isfinite(arr)
        encoded = np.rint(arr[valid] * RAW_PNG_MM_SCALE) + BIT_SCALE_15
        out[valid] = np.clip(encoded, 1, 65535).astype(np.uint16)
        return out

    raise TypeError(f"Unsupported image dtype for raw PNG: {arr.dtype}")


def decode_raw_height_png(image: np.ndarray) -> np.ndarray:
    """Decode a raw height PNG (from :func:`prepare_raw_png` float path) to mm."""
    arr = np.asarray(image)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    mm = (arr.astype(np.float32) - BIT_SCALE_15) / np.float32(RAW_PNG_MM_SCALE)
    mm[arr == 0] = np.nan
    return mm


def save_image_to_file(image: np.ndarray, directory: str, filename: str, archive_params: dict, camera_id=None):
    ''' Saves an image to a specified file directory

    Args:
        image: a numpy array of pixels
        directory: a directory location
        filename: image name including extension
        archive_params: frequency of saving, when to delete, etc.
        camera_id: folder under the date directory
    '''
    if image is None:
        logging.error("Image cannot be none")
        return
    if filename == "":
        logging.error("Filename cannot be empty, please provide a valid file name")
        return

    try:
        save_directory = _archive_save_dir(directory, archive_params, camera_id)
        if save_directory is None:
            return

        image_path = os.path.join(save_directory, filename)
        to_save = prepare_raw_png(image)
        success = imwrite(image_path, to_save)
        if success:
            logging.info(
                "image successfully written to archive shape=%s dtype=%s path=%s",
                getattr(to_save, "shape", None),
                getattr(to_save, "dtype", None),
                image_path,
            )
        else:
            logging.error(f"failed to write image to archive: {image_path}")
    except Exception as e:
        logging.error(f"failed to write image to archive error: {e}")


def archive_image(image: np.ndarray, directory: str, filename: str, archive_params: dict, camera_id=None):
    '''starts a worker thread that will run the save_image_to_file function on a given image

    Args:
        image: a numpy array of pixels
        directory: a directory location
        filename: image name including extension
        '''

    threading.Thread(
        target=save_image_to_file,
        args=(
            image,
            directory,
            filename,
            archive_params,
            camera_id,
        ),
        daemon=True
    ).start()
