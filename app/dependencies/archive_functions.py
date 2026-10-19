import os
import re
import shutil
import logging
import threading
import datetime
import uuid

import numpy as np
from cv2 import imwrite, resize, INTER_AREA, IMWRITE_JPEG_QUALITY

# Float height maps (mm) → 16-bit PNG. 0 = invalid/NaN; else:
#   stored = round(mm * RAW_PNG_MM_SCALE) + 32768
# Recover with decode_raw_height_png() / (img.astype(float) - 32768) / RAW_PNG_MM_SCALE
RAW_PNG_MM_SCALE = 100.0  # 0.01 mm resolution
BIT_SCALE_15 = 32768.0

# Truncated uuid4. 4 hex chars is enough to split two saves that share
# camera, image type, sku, and the same second. The timestamp is already
# in the filename.
ARCHIVE_ID_HEX_LEN = 8

# archive_parameters.archive_format values. 8-bit frames use the configured
# format; 16-bit and float frames (thermal counts, height maps) always stay PNG.
# archive_full_res: true overrides format and scale: full-size PNG (for development data).
ARCHIVE_FORMATS = ("png", "jpg", "jpeg")
JPEG_FORMATS = ("jpg", "jpeg")
DEFAULT_JPEG_QUALITY = 90

# archive_parameters.archive_cleanup: off (default) | dry_run | on.
# Only folders named YYYYMMDD or YYYYMMDD_hh directly under archive_directory
# are ever touched, and only this camera's folders inside them (see _camera_targets).
CLEANUP_MODES = ("off", "dry_run", "on")
DAY_FOLDER = re.compile(r"^(\d{8})(_\d{2})?$")
_cleanup_lock = threading.Lock()
_cleanup_last_run: dict[str, datetime.date] = {}


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
    "uuid",
    "camera_id",
    "camera_type",
    "image_type",
    "sku_id",
    "datetime",
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


def _flag(archive_params: dict | None, key: str) -> bool:
    # See what format to store RGB images in.
    raw = (archive_params or {}).get(key, False)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def archive_extension(image: np.ndarray, archive_params: dict | None) -> str:
    """File extension for an archived frame.

    ``archive_format`` (``png`` by default, or ``jpg``) applies to 8-bit frames.
    16-bit and float frames always stay ``png`` so thermal counts and height
    maps keep their values. ``archive_full_res: true`` always gives ``png``.
    """
    if _flag(archive_params, "archive_full_res"):
        return "png"
    fmt = str((archive_params or {}).get("archive_format") or "png").strip().lower().lstrip(".")
    if fmt not in ARCHIVE_FORMATS:
        logging.warning("Unknown archive_format %r; using png", fmt)
        return "png"
    if fmt in JPEG_FORMATS and np.asarray(image).dtype != np.uint8:
        return "png"
    return fmt


def _archive_scale(archive_params: dict | None) -> float:
    """``archive_scale`` per side for 8-bit frames, in (0, 1]. 1 keeps full size;
    ``archive_full_res: true`` always keeps full size."""
    if _flag(archive_params, "archive_full_res"):
        return 1.0
    raw = (archive_params or {}).get("archive_scale", 1.0)
    try:
        scale = float(raw if raw is not None else 1.0)
    except (TypeError, ValueError):
        logging.warning("Invalid archive_scale %r; saving full size", raw)
        return 1.0
    if not 0 < scale <= 1:
        logging.warning("archive_scale must be in (0, 1], got %r; saving full size", raw)
        return 1.0
    return scale


def _jpeg_quality(archive_params: dict | None) -> int:
    raw = (archive_params or {}).get("jpeg_quality", DEFAULT_JPEG_QUALITY)
    try:
        return min(max(int(raw), 1), 100)
    except (TypeError, ValueError):
        logging.warning("Invalid jpeg_quality %r; using %d", raw, DEFAULT_JPEG_QUALITY)
        return DEFAULT_JPEG_QUALITY


def prepare_archive_image(image: np.ndarray, filename: str, archive_params: dict | None) -> tuple[np.ndarray, list]:
    """Return ``(pixels, imwrite_params)`` for ``filename``'s format.

    8-bit frames are shrunk by ``archive_scale``. 16-bit and float frames go
    through :func:`prepare_raw_png` untouched.
    """
    to_save = prepare_raw_png(image)
    if to_save.dtype != np.uint8:
        return to_save, []

    scale = _archive_scale(archive_params)
    if scale < 1:
        h, w = to_save.shape[:2]
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        to_save = resize(to_save, size, interpolation=INTER_AREA)

    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext in JPEG_FORMATS:
        return to_save, [IMWRITE_JPEG_QUALITY, _jpeg_quality(archive_params)]
    return to_save, []


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


def _cleanup_mode(archive_params: dict | None) -> str:
    raw = (archive_params or {}).get("archive_cleanup", "off")
    if raw is True:
        return "on"
    if raw is False or raw is None:
        return "off"
    mode = str(raw).strip().lower()
    if mode not in CLEANUP_MODES:
        logging.warning("Unknown archive_cleanup %r; cleanup is off", raw)
        return "off"
    return mode


def _positive_number(archive_params: dict | None, key: str) -> float | None:
    """``archive_params[key]`` as a positive float, or None when unset/0/invalid."""
    raw = (archive_params or {}).get(key)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logging.warning("Invalid %s %r; ignoring it for cleanup", key, raw)
        return None
    return value if value > 0 else None


def _folder_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _camera_targets(day_folder: str, camera_id=None) -> list[str]:
    """This camera's folders in one day folder.

    ``{camera_id}`` is the current layout; ``cam_{id}`` and ``{sku}/cam_{id}`` are
    the older one (before the sku folder was dropped), still on disk on sites
    upgraded from it.
    """
    if camera_id is None:
        return [day_folder]
    current = _filename_token(camera_id, "0")
    legacy = f"cam_{camera_id}"
    targets = [p for p in (os.path.join(day_folder, current), os.path.join(day_folder, legacy))
               if os.path.isdir(p)]
    try:
        entries = list(os.scandir(day_folder))
    except OSError:
        return targets
    for entry in entries:
        if entry.is_dir(follow_symlinks=False) and entry.name not in (current, legacy):
            nested = os.path.join(entry.path, legacy)
            if os.path.isdir(nested):
                targets.append(nested)
    return targets


def _remove_if_empty(path: str) -> None:
    try:
        os.rmdir(path)  # only succeeds once nothing else (another camera / sku) is left
    except OSError:
        pass


def _archive_day_folders(base_directory: str, camera_id=None) -> list[tuple[datetime.date, str, list[str]]]:
    """``(day, day_folder, targets)`` oldest first. ``targets`` are this camera's folders in it."""
    found = []
    try:
        entries = list(os.scandir(base_directory))
    except OSError as e:
        logging.error(f"archive cleanup cannot list {base_directory}: {e}")
        return found
    for entry in entries:
        match = DAY_FOLDER.match(entry.name)
        if not match or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            day = datetime.datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        targets = _camera_targets(entry.path, camera_id)
        if targets:
            found.append((day, entry.path, targets))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


def prune_archive(directory, archive_params: dict | None, camera_id=None, today: datetime.date | None = None) -> list[str]:
    """Remove this camera's archive folders older than ``archive_days`` or over ``archive_storage`` GB.

    Oldest days go first. Returns the folders removed (or that would be, in
    ``dry_run``). Today's folder is never removed. ``archive_storage`` counts
    this camera only.
    """
    mode = _cleanup_mode(archive_params)
    if mode == "off" or directory in ("", None):
        return []
    keep_days = _positive_number(archive_params, "archive_days")
    max_gb = _positive_number(archive_params, "archive_storage")
    if keep_days is None and max_gb is None:
        return []

    base_directory = os.fspath(directory)
    today = today or datetime.date.today()
    all_days = _archive_day_folders(base_directory, camera_id)
    folders = [f for f in all_days if f[0] < today]
    chosen = []
    if keep_days is not None:
        cutoff = today - datetime.timedelta(days=int(keep_days))
        chosen = [f for f in folders if f[0] <= cutoff]
        folders = [f for f in folders if f[0] > cutoff]

    if max_gb is not None:
        limit = max_gb * 1e9
        sizes = {f[1]: sum(_folder_size(t) for t in f[2]) for f in folders}
        total = sum(sizes.values())
        total += sum(_folder_size(t) for f in all_days if f[0] >= today for t in f[2])
        while folders and total > limit:
            oldest = folders.pop(0)
            total -= sizes[oldest[1]]
            chosen.append(oldest)

    removed = []
    for _day, day_folder, targets in chosen:
        for target in targets:
            if mode == "dry_run":
                logging.info("archive cleanup (dry_run) would remove %s", target)
                removed.append(target)
                continue
            try:
                shutil.rmtree(target)
                removed.append(target)
                logging.info("archive cleanup removed %s", target)
            except OSError as e:
                logging.error(f"archive cleanup failed to remove {target}: {e}")
                continue
            parent = os.path.dirname(target)
            if parent != day_folder and target != day_folder:
                _remove_if_empty(parent)  # an old-layout sku folder
        if mode == "on" and targets != [day_folder]:
            _remove_if_empty(day_folder)
    if chosen:
        logging.info(
            "archive cleanup (%s) for %s: %d folder(s), keep_days=%s, max_gb=%s",
            mode, camera_id, len(removed), keep_days, max_gb,
        )
    return removed


def maybe_prune_archive(directory, archive_params: dict | None, camera_id=None) -> None:
    """Run :func:`prune_archive` in the background at most once per day per camera."""
    if _cleanup_mode(archive_params) == "off" or directory in ("", None):
        return
    day = datetime.date.today()
    key = f"{os.path.abspath(os.fspath(directory))}|{camera_id}"
    with _cleanup_lock:
        if _cleanup_last_run.get(key) == day:
            return
        _cleanup_last_run[key] = day

    def _run():
        try:
            prune_archive(directory, archive_params, camera_id, today=day)
        except Exception as e:
            logging.error(f"archive cleanup error: {e}")

    threading.Thread(target=_run, name="archive-cleanup", daemon=True).start()


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
        filename: image name including extension (.png or .jpg, see archive_extension)
        archive_params: frequency of saving, format and size, cleanup, etc.
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
        maybe_prune_archive(directory, archive_params, camera_id)

        image_path = os.path.join(save_directory, filename)
        to_save, write_params = prepare_archive_image(image, filename, archive_params)
        success = imwrite(image_path, to_save, write_params)
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
