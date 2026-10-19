import datetime
import os
import threading

import cv2
import numpy as np
import pytest

from dependencies import archive_functions as af

COMPACT = {"archive_format": "jpg", "archive_scale": 0.5, "jpeg_quality": 90}
TODAY = datetime.date(2026, 10, 13)


def colour_frame():
    return np.random.default_rng(0).integers(0, 255, (2448, 2048, 3), dtype=np.uint8)


# --- format and size ---------------------------------------------------------------------------

def test_an_8bit_frame_is_archived_as_the_configured_jpeg():
    assert af.archive_extension(colour_frame(), COMPACT) == "jpg"


def test_png_is_the_default():
    assert af.archive_extension(colour_frame(), {}) == "png"
    assert af.archive_extension(colour_frame(), None) == "png"


def test_16bit_frames_stay_png_even_when_jpeg_is_configured():
    thermal = np.zeros((480, 640), np.uint16)
    assert af.archive_extension(thermal, COMPACT) == "png"


def test_an_unknown_format_falls_back_to_png():
    assert af.archive_extension(colour_frame(), {"archive_format": "tiff"}) == "png"


def test_full_res_mode_overrides_format_and_scale():
    params = dict(COMPACT, archive_full_res=True)
    assert af.archive_extension(colour_frame(), params) == "png"
    pixels, write_params = af.prepare_archive_image(colour_frame(), "x.png", params)
    assert pixels.shape == (2448, 2048, 3) and write_params == []


def test_full_res_accepts_a_yaml_string():
    assert af.archive_extension(colour_frame(), dict(COMPACT, archive_full_res="true")) == "png"


def test_an_8bit_frame_is_shrunk_and_given_the_jpeg_quality():
    pixels, write_params = af.prepare_archive_image(colour_frame(), "x.jpg", COMPACT)
    assert pixels.shape == (1224, 1024, 3)
    assert write_params == [cv2.IMWRITE_JPEG_QUALITY, 90]


def test_16bit_frames_are_never_resized():
    thermal = np.full((480, 640), 30000, np.uint16)
    pixels, write_params = af.prepare_archive_image(thermal, "x.png", COMPACT)
    assert pixels.shape == (480, 640) and pixels.dtype == np.uint16 and write_params == []


@pytest.mark.parametrize("scale", [0, -1, 2, "big"])
def test_an_invalid_scale_saves_full_size(scale):
    pixels, _ = af.prepare_archive_image(colour_frame(), "x.jpg", dict(COMPACT, archive_scale=scale))
    assert pixels.shape == (2448, 2048, 3)


def test_save_writes_a_small_jpeg(tmp_path):
    af.save_image_to_file(colour_frame(), str(tmp_path), "frame.jpg", dict(COMPACT, archive_freq="daily"), "colour")
    (path,) = list(tmp_path.rglob("frame.jpg"))
    saved = cv2.imread(str(path))
    assert saved.shape == (1224, 1024, 3)


# --- cleanup -----------------------------------------------------------------------------------

def make_day(base, name, camera="colour", size=1000):
    folder = base / name / camera
    folder.mkdir(parents=True)
    (folder / "img.jpg").write_bytes(b"x" * size)
    return folder


def test_cleanup_removes_days_past_archive_days_but_keeps_today(tmp_path):
    old = make_day(tmp_path, "20260920")
    kept = make_day(tmp_path, "20261005")
    today = make_day(tmp_path, "20261013")
    removed = af.prune_archive(str(tmp_path), {"archive_cleanup": "on", "archive_days": 14}, "colour", today=TODAY)
    assert removed == [str(old)]
    assert not (tmp_path / "20260920").exists()      # the empty day folder goes too
    assert kept.exists() and today.exists()


def test_cleanup_removes_the_oldest_days_until_under_archive_storage(tmp_path):
    first = make_day(tmp_path, "20261010", size=400)
    second = make_day(tmp_path, "20261011", size=400)
    third = make_day(tmp_path, "20261012", size=400)
    params = {"archive_cleanup": "on", "archive_storage": 1e-6}   # GB: 1000 bytes
    removed = af.prune_archive(str(tmp_path), params, "colour", today=TODAY)
    assert removed == [str(first)]
    assert not first.exists() and second.exists() and third.exists()


def test_dry_run_reports_but_deletes_nothing(tmp_path):
    old = make_day(tmp_path, "20260920")
    removed = af.prune_archive(str(tmp_path), {"archive_cleanup": "dry_run", "archive_days": 14}, "colour",
                               today=TODAY)
    assert removed == [str(old)] and old.exists()


@pytest.mark.parametrize("mode", ["off", None, False, "nonsense"])
def test_cleanup_is_off_unless_turned_on(tmp_path, mode):
    old = make_day(tmp_path, "20260920")
    assert af.prune_archive(str(tmp_path), {"archive_cleanup": mode, "archive_days": 14}, "colour",
                            today=TODAY) == []
    assert old.exists()


def test_cleanup_touches_only_this_camera_and_only_date_folders(tmp_path):
    mine = make_day(tmp_path, "20260920", camera="colour")
    other_camera = make_day(tmp_path, "20260920", camera="thermal")
    other_folder = tmp_path / "keep_me" / "colour"
    other_folder.mkdir(parents=True)
    af.prune_archive(str(tmp_path), {"archive_cleanup": "on", "archive_days": 14}, "colour", today=TODAY)
    assert not mine.exists()
    assert other_camera.exists() and other_folder.exists()
    assert (tmp_path / "20260920").exists()           # still holds the other camera


def test_old_layout_sku_and_cam_folders_are_cleaned_too(tmp_path):
    # before the sku folder was dropped: YYYYMMDD/{sku}/cam_{id} (and YYYYMMDD/cam_{id})
    sirloin = make_day(tmp_path, "20260920", camera="sirloin_standard/cam_colour_1")
    ribeye = make_day(tmp_path, "20260920", camera="ribeye_standard/cam_colour_1")
    flat = make_day(tmp_path, "20260921", camera="cam_colour_1")
    other = make_day(tmp_path, "20260922", camera="sirloin_standard/cam_colour_2")
    current = make_day(tmp_path, "20260923", camera="colour_1")
    kept = make_day(tmp_path, "20261012", camera="sirloin_standard/cam_colour_1")
    removed = af.prune_archive(str(tmp_path), {"archive_cleanup": "on", "archive_days": 14}, "colour_1",
                               today=TODAY)
    assert sorted(removed) == sorted(map(str, (sirloin, ribeye, flat, current)))
    assert not (tmp_path / "20260920").exists()       # empty sku and day folders go too
    assert not (tmp_path / "20260921").exists()
    assert other.exists() and kept.exists()


def test_old_layout_counts_towards_archive_storage(tmp_path):
    old = make_day(tmp_path, "20261010", camera="sirloin_standard/cam_colour_1", size=600)
    new = make_day(tmp_path, "20261012", camera="colour_1", size=600)
    params = {"archive_cleanup": "on", "archive_storage": 1e-6}   # GB: 1000 bytes
    assert af.prune_archive(str(tmp_path), params, "colour_1", today=TODAY) == [str(old)]
    assert new.exists()


def test_hourly_folders_are_cleaned_by_their_day(tmp_path):
    old = make_day(tmp_path, "20260920_08", camera="thermal")
    kept = make_day(tmp_path, "20261012_08", camera="thermal")
    af.prune_archive(str(tmp_path), {"archive_cleanup": "on", "archive_days": 14}, "thermal", today=TODAY)
    assert not old.exists() and kept.exists()


def test_cleanup_runs_once_a_day_per_camera(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(af, "prune_archive", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(af, "_cleanup_last_run", {})
    params = {"archive_cleanup": "on", "archive_days": 14}
    for _ in range(3):
        af.maybe_prune_archive(str(tmp_path), params, "colour")
    af.maybe_prune_archive(str(tmp_path), params, "thermal")
    for thread in [t for t in threading.enumerate() if t.name == "archive-cleanup"]:
        thread.join(timeout=5)
    assert len(calls) == 2


def test_archived_filenames_get_the_configured_extension():
    name = af.build_archive_filename(["camera_id"], {"camera_id": "colour"},
                                     ext=af.archive_extension(colour_frame(), COMPACT))
    assert os.path.splitext(name)[1] == ".jpg"
