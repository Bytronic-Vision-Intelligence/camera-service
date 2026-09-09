"""Direct Ethernet driver for the Keyence LJ-S8000 series head.

Talks straight to the LJ-S640 sensor head using KEYENCE's LJ-S Communication
Library (``LJS8_IF.dll`` via the vendored ``LJSwrap`` ctypes wrapper). There
is no controller in this deployment -- the head is driven externally from
its own TRG terminal and streams profiles back over the high-speed data
communication channel, which this driver decodes into height maps (mm).
"""

from __future__ import annotations

import ctypes
import logging
import time
from queue import Empty, Queue
from threading import Event
from typing import Any, Optional

import numpy as np

from dependencies import service_settings

from . import settings_ljs
from .cameras import CameraHeightMap
from .hardware_trigger import report_camera_loss

logger = logging.getLogger(__name__)

# LJS8IF_GetAttentionStatus bitmask (RM §8.2.3).
_SCMD_READY = 0x0008
_TRG_READY = 0x0020
_MEM_FULL = 0x0040

# RM §7.3.1 dwNotify bits, and RM §12.2 step (5): bit 0 specifically means
# "stop by command" -- i.e. *we* called LJS8IF_StopHighSpeedDataCommunication
# (the only place that happens is stop_acquisition(), always part of our own
# shutdown). That is expected, not a failure, so it gets its own quiet exit
# rather than a false CAMERA LOSS on every clean stop. Bits 1/2/3/8 mean the
# stream was stopped or interrupted by something *other* than us (settings or
# program switched, forced stop, memory cleared mid-send) and are genuine
# anomalies. 0x10000 ("one image fully sent") is a normal data notification.
_EXPECTED_STOP_BIT = 0x1
_UNEXPECTED_STOP_MASK = 0x2 | 0x4 | 0x8 | 0x100


class _StreamStopped:
    """Sentinel: the high-speed stream ended because we asked it to (RM
    §7.3.1 notify bit 0 / §12.2 step 5), not because anything went wrong."""


try:
    from . import LJSwrap
except (ImportError, OSError) as exc:  # pragma: no cover - platform/runtime dependent
    # OSError: cdll.LoadLibrary raises this (not ImportError) when the VC++
    # runtime is missing. Deferred to connect_to_camera() so that importing
    # this module -- and therefore CameraLibrary/__init__.py, which every
    # camera backend goes through -- never fails just because this machine
    # doesn't have the LJ-S runtime installed.
    LJSwrap = None
    _IMPORT_ERROR: Optional[Exception] = exc
else:
    _IMPORT_ERROR = None


def _parse_ipv4(host: str) -> tuple:
    parts = host.strip().split(".")
    if len(parts) != 4:
        raise ValueError(f"Invalid IPv4 address for LJ-S head: {host!r}")
    try:
        octets = tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"Invalid IPv4 address for LJ-S head: {host!r}") from exc
    if any(not 0 <= o <= 255 for o in octets):
        raise ValueError(f"Invalid IPv4 address for LJ-S head: {host!r}")
    return octets


def _hex(value: int) -> str:
    return f"0x{int(value) & 0xFFFFFFFF:08x}"


def decode_height_mm(raw: np.ndarray, dw_pitch_z: int) -> np.ndarray:
    """Convert raw LJ-S height counts (uint16) to millimetres, NaN = invalid.

    RM §8.2.6: ``height_um = (stored - 32768) * dwPitchZ / 100``, where
    ``dwPitchZ`` is in units of 0.01um (e.g. LJ-S640 -> 1020 -> 0.0102
    mm/count). A stored value of 0 marks invalid/dead-zone data; it is
    masked to NaN *after* the arithmetic, since ``(0 - 32768) * pitch`` is
    otherwise a plausible-looking height.
    """
    raw = np.asarray(raw)
    diff = raw.astype(np.float64) - 32768.0
    mm = (diff * (float(dw_pitch_z) / 100000.0)).astype(np.float32)
    mm[raw == 0] = np.nan
    return mm


class LJSCamera(CameraHeightMap):
    """
    LJ-S8000 3D laser profile head, compatible with the CameraHeightMap interface.

    Usage:
        ljs = LJSCamera()                 # reads camera.ljs.* from config.yaml
        ljs.connect_to_camera()           # opens the head, applies settings,
                                           # and starts the high-speed stream
        height_mm = ljs.capture_image()   # 2D float32 array in mm, NaN = no data
        ljs.disconnect_camera()

    With ``trigger.trigger_type: internal``, ``main.py`` subscribes to MQTT and
    calls ``capture_image()`` (software ``LJS8IF_Trigger``). With ``external``,
    the head is triggered from its TRG terminal and ``main.py`` drains
    ``wait_for_frame()`` on a background thread.

    Any keyword arguments override the matching ``camera.ljs.*`` config key
    (host, port, high_speed_port, device_id, program, timeout_s,
    use_image_filter, interpolate_y, settings).
    """

    def __init__(self, **overrides: Any) -> None:
        super().__init__()
        cfg = dict(service_settings.get_section("camera").get("ljs") or {})
        cfg.update(overrides)

        self.host = str(cfg.get("host", "192.168.0.1"))
        self.port = int(cfg.get("port", 24691))
        self.high_speed_port = int(cfg.get("high_speed_port", 24692))
        if self.port == self.high_speed_port:
            raise ValueError(
                "camera.ljs.port and camera.ljs.high_speed_port must differ "
                f"(both are {self.port})"
            )
        self.device_id = int(cfg.get("device_id", 0))
        self.program = int(cfg.get("program", 0))
        if not 0 <= self.program <= 15:
            raise ValueError(f"camera.ljs.program must be 0-15, got {self.program}")
        self.timeout_s = float(cfg.get("timeout_s", 5.0))
        self.use_image_filter = bool(cfg.get("use_image_filter", False))
        self.interpolate_y = int(cfg.get("interpolate_y", 1))
        if not 1 <= self.interpolate_y <= 8:
            raise ValueError(
                f"camera.ljs.interpolate_y must be 1-8, got {self.interpolate_y}"
            )
        self.settings = dict(cfg.get("settings") or {})

        self.cam = None
        self._connected = False
        self._streaming = False
        self._callback_ref = None
        self._x_points = 0
        self._y_lines = 0
        self._pitch_z = 0
        self._scratch: Optional[np.ndarray] = None
        # Filled by the DLL's receive thread (via _on_profiles), drained by
        # wait_for_frame()/capture_image(). Holds either a raw uint16
        # (y_lines, x_points) ndarray, or an exception describing why the
        # stream stopped.
        self._raw_queue: Queue = Queue()

    # -- connection lifecycle ------------------------------------------------

    def connect_to_camera(self, timeout: Optional[float] = None) -> LJSCamera:
        if LJSwrap is None:
            raise RuntimeError(
                "LJ-S communication library unavailable: LJS8_IF.dll failed to "
                "load (install the Visual C++ runtime; see "
                "CameraLibrary/runtime/VC_V143/VC_redist.x64.exe)"
            ) from _IMPORT_ERROR

        if timeout is not None:
            self.timeout_s = float(timeout)

        logger.info(
            "Connecting to LJ-S head at %s:%s (high-speed port %s, program %d)",
            self.host, self.port, self.high_speed_port, self.program,
        )

        if hasattr(LJSwrap, "LJS8IF_Initialize"):
            res = LJSwrap.LJS8IF_Initialize()
            if res != 0:
                raise RuntimeError(f"LJS8IF_Initialize failed: {_hex(res)}")

        eth_cfg = LJSwrap.LJS8IF_ETHERNET_CONFIG()
        for i, octet in enumerate(_parse_ipv4(self.host)):
            eth_cfg.abyIpAddress[i] = octet
        eth_cfg.wPortNo = self.port

        res = LJSwrap.LJS8IF_EthernetOpen(self.device_id, eth_cfg)
        if res != 0:
            self._safe_finalize_dll()
            raise RuntimeError(f"LJS8IF_EthernetOpen failed: {_hex(res)}")

        try:
            self._setup_after_open(eth_cfg)
        except Exception:
            self._cleanup_after_failed_connect()
            raise

        self._connected = True
        self.cam = self.device_id
        logger.info(
            "LJ-S head connected: %dx%d points, Z pitch=%d (0.01um units)",
            self._x_points, self._y_lines, self._pitch_z,
        )
        return self

    def _setup_after_open(self, eth_cfg) -> None:
        """Everything after EthernetOpen succeeds: program -> settings ->
        laser -> high-speed init/prestart/start (RM §12.2 setup order)."""
        res = LJSwrap.LJS8IF_ChangeActiveProgram(self.device_id, self.program)
        if res != 0:
            raise RuntimeError(f"LJS8IF_ChangeActiveProgram failed: {_hex(res)}")

        # Settings must land (WRITE -> ReflectSetting -> RUNNING) before the
        # high-speed stream starts, or the stream auto-stops the moment
        # settings change underneath it (RM §7.3.1 notify bit 1).
        self._apply_settings()

        res = LJSwrap.LJS8IF_ControlLaser(self.device_id, 1)
        if res != 0:
            raise RuntimeError(f"LJS8IF_ControlLaser(on) failed: {_hex(res)}")

        self._callback_ref = LJSwrap.LJS8IF_CALLBACK_SIMPLE_ARRAY(self._on_profiles)

        res = LJSwrap.LJS8IF_InitializeHighSpeedDataCommunicationSimpleArray(
            self.device_id, eth_cfg, self.high_speed_port, self._callback_ref, self.device_id,
        )
        if res != 0:
            raise RuntimeError(
                f"LJS8IF_InitializeHighSpeedDataCommunicationSimpleArray failed: {_hex(res)}"
            )

        pre_req = LJSwrap.LJS8IF_HIGH_SPEED_PRE_START_REQ()
        # "From next data" (RM §12.1): 0/1 would replay whatever the head
        # already buffered before we connected, so the first capture would
        # be stale.
        pre_req.bySendPosition = 2
        height_info = LJSwrap.LJS8IF_HEIGHT_IMAGE_INFO()
        res = LJSwrap.LJS8IF_PreStartHighSpeedDataCommunication(
            self.device_id, pre_req, int(self.use_image_filter), height_info,
        )
        if res != 0:
            raise RuntimeError(
                f"LJS8IF_PreStartHighSpeedDataCommunication failed: {_hex(res)}"
            )

        x_points = int(height_info.wXPointNum)
        y_lines = int(height_info.wYLineNum)
        pitch_z = int(height_info.dwPitchZ)
        if x_points <= 0 or y_lines <= 0:
            raise RuntimeError(
                f"LJ-S head reported an empty image geometry ({x_points}x{y_lines})"
            )

        self._x_points = x_points
        self._y_lines = y_lines
        self._pitch_z = pitch_z
        # Allocated once from the geometry PreStart reports, reused by every
        # callback (see _on_profiles) instead of allocating ~21MB per scan.
        self._scratch = np.empty(x_points * y_lines, dtype=np.uint16)

        res = LJSwrap.LJS8IF_StartHighSpeedDataCommunication(self.device_id)
        if res != 0:
            raise RuntimeError(f"LJS8IF_StartHighSpeedDataCommunication failed: {_hex(res)}")
        self._streaming = True

    def _apply_settings(self) -> None:
        if not self.settings:
            return

        program_type = settings_ljs.program_type_byte(self.program)
        err = ctypes.c_uint()

        for key, value in self.settings.items():
            spec = settings_ljs.SETTINGS_REGISTRY.get(key)
            if spec is None:
                logger.warning("Unknown LJS setting %r (value=%r); skipping", key, value)
                continue
            target, encode = spec
            try:
                payload = encode(value)
            except ValueError as exc:
                raise ValueError(f"Invalid LJS setting {key}={value!r}: {exc}") from exc

            target_setting = LJSwrap.LJS8IF_TARGET_SETTING()
            target_setting.byType = program_type
            target_setting.byCategory = target.category
            target_setting.byItem = target.item
            target_setting.byTarget1 = target.target1
            target_setting.byTarget2 = target.target2
            target_setting.byTarget3 = target.target3
            target_setting.byTarget4 = target.target4

            buf = (ctypes.c_ubyte * len(payload))(*payload)
            res = LJSwrap.LJS8IF_SetSetting(
                self.device_id, settings_ljs.DEPTH_WRITE, target_setting, buf, len(payload), err,
            )
            if res != 0:
                raise RuntimeError(
                    f"LJS8IF_SetSetting({key}={value!r}) failed: {_hex(res)} "
                    f"(detail error {_hex(err.value)})"
                )
            logger.debug("Queued LJS setting %s=%r for program %d", key, value, self.program)

        # One Reflect commits every WRITE-area setting above at once, so a
        # transiently-inconsistent pair (e.g. light control upper below
        # lower) never gets validated mid-batch (RM §8.2.8.2 usage example 1).
        res = LJSwrap.LJS8IF_ReflectSetting(self.device_id, settings_ljs.DEPTH_RUNNING, err)
        if res != 0:
            raise RuntimeError(
                f"LJS8IF_ReflectSetting failed: {_hex(res)} (detail error {_hex(err.value)})"
            )
        logger.info("Applied %d LJS setting(s) to program %d", len(self.settings), self.program)

    def _cleanup_after_failed_connect(self) -> None:
        try:
            LJSwrap.LJS8IF_FinalizeHighSpeedDataCommunication(self.device_id)
        except Exception:
            logger.debug(
                "Cleanup: LJS8IF_FinalizeHighSpeedDataCommunication failed", exc_info=True
            )
        try:
            LJSwrap.LJS8IF_CommunicationClose(self.device_id)
        except Exception:
            logger.debug("Cleanup: LJS8IF_CommunicationClose failed", exc_info=True)
        self._safe_finalize_dll()

    def _safe_finalize_dll(self) -> None:
        if LJSwrap is not None and hasattr(LJSwrap, "LJS8IF_Finalize"):
            try:
                LJSwrap.LJS8IF_Finalize()
            except Exception:
                logger.debug("LJS8IF_Finalize failed", exc_info=True)

    # -- callback (DLL receive thread) ---------------------------------------

    def _on_profiles(
        self,
        p_header,
        p_height,
        p_lumi,
        luminance_enable,
        xpointnum,
        profnum,
        notify,
        user,
    ) -> None:
        """LJS8IF_CALLBACK_SIMPLE_ARRAY trampoline (RM §7.3).

        Runs on the DLL's own receive thread, in lockstep with data
        reception -- do the least possible work here (a single memmove into
        a preallocated buffer) and never let an exception unwind into the C
        caller. All height decoding happens later, on the consumer thread
        (wait_for_frame/capture_image).
        """
        try:
            notify = int(notify)
            if notify & _UNEXPECTED_STOP_MASK:
                self._raw_queue.put(
                    RuntimeError(
                        f"LJ-S high-speed data communication stopped unexpectedly "
                        f"(notify={_hex(notify)})"
                    )
                )
                return
            if notify & _EXPECTED_STOP_BIT:
                self._raw_queue.put(_StreamStopped())
                return

            profnum = int(profnum)
            if profnum <= 0:
                return

            xpointnum = int(xpointnum)
            count = xpointnum * profnum
            scratch = self._scratch
            if scratch is None or count > scratch.size:
                logger.error(
                    "LJ-S callback reported %d points but scratch buffer holds %s; dropping frame",
                    count, "none" if scratch is None else scratch.size,
                )
                return

            ctypes.memmove(
                scratch.ctypes.data, p_height, count * ctypes.sizeof(ctypes.c_ushort)
            )
            # .copy() hands the consumer thread an independent snapshot --
            # `scratch` itself gets overwritten by the next callback.
            self._raw_queue.put(scratch[:count].reshape(profnum, xpointnum).copy())
        except Exception:
            logger.error("Unhandled error in LJ-S profile callback", exc_info=True)

    def _decode(self, raw: np.ndarray) -> np.ndarray:
        height_mm = decode_height_mm(raw, self._pitch_z)
        if self.interpolate_y > 1:
            height_mm = np.repeat(height_mm, self.interpolate_y, axis=0)
        return height_mm

    # -- capture ---------------------------------------------------------------

    def wait_for_frame(self, queue: Queue, stop_event: Event) -> None:
        """Passively relay one decoded height map per completed scan.

        The head free-runs off its own TRG terminal; this loop never issues
        a software trigger of its own -- doing so here would race the
        hardware trigger and free-run the head as fast as this loop spins.
        """
        if not self._connected:
            raise RuntimeError("LJSCamera is not connected")

        while not stop_event.is_set():
            try:
                item = self._raw_queue.get(timeout=0.5)
            except Empty:
                continue

            if isinstance(item, _StreamStopped):
                logger.info("LJ-S high-speed data communication stopped (expected)")
                return

            if isinstance(item, BaseException):
                report_camera_loss(item, queue=queue)
                return

            try:
                queue.put(self._decode(item))
            except Exception:
                logger.error("Failed to decode LJ-S height data", exc_info=True)

    def capture_image(self, timeout_s: Optional[float] = None) -> np.ndarray:
        """Software-trigger one scan and return its height map (mm, NaN = invalid).

        ``main.py`` calls this for ``camera_type: ljs`` when
        ``trigger.trigger_type`` is ``internal`` (MQTT-driven).
        """
        if not self._connected:
            raise RuntimeError("LJSCamera is not connected")

        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        deadline = time.monotonic() + timeout

        status = ctypes.c_ushort()
        while True:
            res = LJSwrap.LJS8IF_GetAttentionStatus(self.device_id, status)
            if res == 0 and (status.value & _TRG_READY):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("LJ-S head did not report TRG_READY before timeout")
            time.sleep(0.01)

        res = LJSwrap.LJS8IF_Trigger(self.device_id)
        if res != 0:
            # e.g. 0x80A0 if the laser isn't enabled -- see ControlLaser(1) in connect.
            raise RuntimeError(f"LJS8IF_Trigger failed: {_hex(res)}")

        remaining = max(deadline - time.monotonic(), 0.1)
        try:
            item = self._raw_queue.get(timeout=remaining)
        except Empty:
            raise TimeoutError("Timed out waiting for LJ-S profile data") from None
        if isinstance(item, _StreamStopped):
            raise RuntimeError("LJ-S high-speed data communication was stopped")
        if isinstance(item, BaseException):
            raise item
        return self._decode(item)

    def get_attention_status(self) -> int:
        """Raw LJS8IF_GetAttentionStatus bitmask (_TRG_READY / _SCMD_READY / _MEM_FULL)."""
        if not self._connected:
            raise RuntimeError("LJSCamera is not connected")
        status = ctypes.c_ushort()
        res = LJSwrap.LJS8IF_GetAttentionStatus(self.device_id, status)
        if res != 0:
            raise RuntimeError(f"LJS8IF_GetAttentionStatus failed: {_hex(res)}")
        return status.value

    # -- shutdown ---------------------------------------------------------------

    def stop_acquisition(self) -> None:
        """Stop the high-speed stream. Called by main.py before disconnect_camera
        so the head stops pushing profiles we're no longer consuming."""
        if LJSwrap is None or not self._streaming:
            return
        logger.info("Stopping LJ-S high-speed data communication")
        try:
            LJSwrap.LJS8IF_StopHighSpeedDataCommunication(self.device_id)
        except Exception:
            logger.debug("LJS8IF_StopHighSpeedDataCommunication failed", exc_info=True)
        finally:
            self._streaming = False

    def disconnect_camera(self, camera=None) -> None:
        """Tear down the connection. Idempotent -- safe to call more than once
        (and ``camera`` is accepted-but-ignored; state lives on ``self``)."""
        if LJSwrap is None or not self._connected:
            return
        logger.info("Disconnecting LJ-S head")
        self.stop_acquisition()
        try:
            LJSwrap.LJS8IF_FinalizeHighSpeedDataCommunication(self.device_id)
        except Exception:
            logger.debug("LJS8IF_FinalizeHighSpeedDataCommunication failed", exc_info=True)
        try:
            LJSwrap.LJS8IF_ControlLaser(self.device_id, 0)
        except Exception:
            logger.debug("LJS8IF_ControlLaser(off) failed", exc_info=True)
        try:
            LJSwrap.LJS8IF_CommunicationClose(self.device_id)
        except Exception:
            logger.debug("LJS8IF_CommunicationClose failed", exc_info=True)
        self._safe_finalize_dll()
        self._connected = False
        self._callback_ref = None
        self.cam = None
        logger.info("LJ-S head disconnected")
