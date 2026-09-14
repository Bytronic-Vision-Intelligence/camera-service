"""Encode/decode tables for LJ-S8000 head settings (Communication Library RM §11.3).

Kept separate from ``cameras_ljs.py`` so the byte-layout knowledge for each
setting item lives in one place and can be unit tested without touching
ctypes/the DLL. Values here describe *program-scoped* settings only (the
ones addressed via ``byType = 0x10 + program_no``); see RM §11.3 for the
full table if more items are needed later.

Every payload here is 4 bytes: the value in byte 0, bytes 1-3 fixed to 0,
little-endian -- except ``trigger_delay_ms`` which spans bytes 0-1 (a u16)
with bytes 2-3 reserved. This matches the worked example in RM p.39: a
trigger delay of 500 ms encodes to ``F4 01 00 00``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Callable

# -- Depth (RM §8.2.8.2 / LJS8IF_SETTING_DEPTH) -----------------------------
#
# WRITE holds edits that don't affect operation until reflected; RUNNING is
# what the head is actually using; SAVE persists across power cycles.
DEPTH_WRITE = 0x00
DEPTH_RUNNING = 0x01
DEPTH_SAVE = 0x02

# -- Setting "Type" byte (RM §11.1) -----------------------------------------
ENVIRONMENT_TYPE = 0x01
COMMON_MEASURE_TYPE = 0x02
_PROGRAM_TYPE_BASE = 0x10  # program n -> 0x10 + n (0x10..0x1F)


def program_type_byte(program_no: int) -> int:
    """Return the ``byType`` value addressing settings for program ``program_no`` (0-15)."""
    program_no = int(program_no)
    if not 0 <= program_no <= 15:
        raise ValueError(f"program number must be 0-15, got {program_no}")
    return _PROGRAM_TYPE_BASE + program_no


@dataclass(frozen=True)
class SettingTarget:
    """The (byCategory, byItem, byTarget1-4) half of an LJS8IF_TARGET_SETTING."""

    category: int
    item: int
    target1: int = 0
    target2: int = 0
    target3: int = 0
    target4: int = 0


# -- Payload encoders (RM §11.3) --------------------------------------------

def _pack_u8(value: int, lo: int, hi: int, name: str) -> bytes:
    value = int(value)
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be {lo}-{hi}, got {value}")
    return struct.pack("<BBBB", value, 0, 0, 0)


def encode_trigger_delay_ms(value: int) -> bytes:
    """Trigger delay in ms, 0-999 (Category 01h, Item 00h)."""
    value = int(value)
    if not 0 <= value <= 999:
        raise ValueError(f"trigger_delay_ms must be 0-999, got {value}")
    return struct.pack("<HH", value, 0)


# Exposure time is an enum index (0-15), not a free microsecond value
# (Category 04h, Item 00h). Note per RM: LJ-S015/025/040 cannot use index 14
# (4800us) -- this table doesn't head-model-gate that; the head itself will
# reject it via LJS8IF_SetSetting's detailed error if unsupported.
EXPOSURE_TIME_US: tuple[int, ...] = (
    15, 30, 60, 80, 120, 160, 210, 240, 320, 380, 480, 640, 960, 1700, 4800, 9600,
)


def encode_exposure_time_us(value: int) -> bytes:
    try:
        index = EXPOSURE_TIME_US.index(int(value))
    except ValueError:
        raise ValueError(
            f"exposure_time_us must be one of {EXPOSURE_TIME_US}, got {value!r}"
        ) from None
    return struct.pack("<BBBB", index, 0, 0, 0)


def encode_dynamic_range(value: int) -> bytes:
    """CMOS dynamic range, 1-9 (Category 04h, Item 01h)."""
    return _pack_u8(value, 1, 9, "dynamic_range")


_LIGHT_CONTROL_MODES = {"MANUAL": 0, "AUTO": 1, "SLOPE": 2}


def encode_light_control_mode(value: Any) -> bytes:
    """Light intensity control mode: MANUAL/AUTO/SLOPE (Category 04h, Item 02h)."""
    if isinstance(value, str):
        key = value.strip().upper()
        if key not in _LIGHT_CONTROL_MODES:
            raise ValueError(
                f"light_control_mode must be one of {sorted(_LIGHT_CONTROL_MODES)}, got {value!r}"
            )
        mode = _LIGHT_CONTROL_MODES[key]
    else:
        mode = int(value)
        if mode not in _LIGHT_CONTROL_MODES.values():
            raise ValueError(f"light_control_mode must be 0-2, got {mode}")
    return struct.pack("<BBBB", mode, 0, 0, 0)


def encode_light_control_limit(value: int) -> bytes:
    """Light intensity control upper/lower limit, 1-99 (Category 04h, Item 03h/04h)."""
    return _pack_u8(value, 1, 99, "light_control limit")


def encode_detection_sensitivity(value: int) -> bytes:
    """Peak detection sensitivity, 1 (low) - 5 (high) (Category 05h, Item 00h)."""
    return _pack_u8(value, 1, 5, "detection_sensitivity")


# -- Targets (RM §11.3, program-scoped settings) -----------------------------

TRIGGER_DELAY_MS = SettingTarget(category=0x01, item=0x00)
EXPOSURE_TIME = SettingTarget(category=0x04, item=0x00)
DYNAMIC_RANGE = SettingTarget(category=0x04, item=0x01)
LIGHT_CONTROL_MODE = SettingTarget(category=0x04, item=0x02)
LIGHT_CONTROL_UPPER = SettingTarget(category=0x04, item=0x03)
LIGHT_CONTROL_LOWER = SettingTarget(category=0x04, item=0x04)
DETECTION_SENSITIVITY = SettingTarget(category=0x05, item=0x00)

# Config key (service.camera_settings.<key>) -> (target, encoder).
SETTINGS_REGISTRY: dict[str, tuple[SettingTarget, Callable[[Any], bytes]]] = {
    "trigger_delay_ms": (TRIGGER_DELAY_MS, encode_trigger_delay_ms),
    "exposure_time_us": (EXPOSURE_TIME, encode_exposure_time_us),
    "dynamic_range": (DYNAMIC_RANGE, encode_dynamic_range),
    "light_control_mode": (LIGHT_CONTROL_MODE, encode_light_control_mode),
    "light_control_upper": (LIGHT_CONTROL_UPPER, encode_light_control_limit),
    "light_control_lower": (LIGHT_CONTROL_LOWER, encode_light_control_limit),
    "detection_sensitivity": (DETECTION_SENSITIVITY, encode_detection_sensitivity),
}
