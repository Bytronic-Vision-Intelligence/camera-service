import os
import logging
import threading
import datetime

import numpy as np
from cv2 import imwrite

# Float height maps (mm) → 16-bit PNG. 0 = invalid/NaN; else:
#   stored = round(mm * RAW_PNG_MM_SCALE) + 32768
# Recover with decode_raw_height_png() / (img.astype(float) - 32768) / RAW_PNG_MM_SCALE
RAW_PNG_MM_SCALE = 100.0  # 0.01 mm resolution


def _archive_save_dir(directory, archive_params: dict, camera_id=None) -> str | None:
    """Build dated/cam subfolder under archive_directory. Returns None on bad input."""
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
        path_parts.append(f"cam_{camera_id}")
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
        encoded = np.rint(arr[valid] * RAW_PNG_MM_SCALE) + 32768.0
        out[valid] = np.clip(encoded, 1, 65535).astype(np.uint16)
        return out

    raise TypeError(f"Unsupported image dtype for raw PNG: {arr.dtype}")


def decode_raw_height_png(image: np.ndarray) -> np.ndarray:
    """Decode a raw height PNG (from :func:`prepare_raw_png` float path) to mm."""
    arr = np.asarray(image)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    mm = (arr.astype(np.float32) - 32768.0) / np.float32(RAW_PNG_MM_SCALE)
    mm[arr == 0] = np.nan
    return mm


def save_image_to_file(image: np.ndarray, directory: str, filename: str, archive_params: dict, camera_id=None):
    ''' Saves an image to a specified file directory

    Args:
        image: a numpy array of pixels
        directory: a directory location
        filename: the name of the image
        archive_params: frequency of saving, when to delete, etc.
        camera_id: optional id used as save_dir/date/cam{id}
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

        image_path = os.path.join(save_directory, f"{filename}.png")
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
        filename: the name of the image
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
