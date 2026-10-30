"""LMI Gocator through LMI's GoSdk: snapshot sensors (e.g. Gocator 3210) and line profilers
(e.g. Gocator 2150), each giving one height map per software trigger."""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import time
from typing import Optional

import numpy as np

from ..archive_functions import prepare_raw_png
from .cameras import CameraHeightMap

logger = logging.getLogger(__name__)

SDK_CANDIDATES = [
    Path(os.environ["GOCATOR_SDK"]) if os.environ.get("GOCATOR_SDK") else None,
    Path(r"C:\Program Files\LMI Technologies\GO_SDK\bin\win64"),
]

kOK = 1   # kStatus success (kApiDef.h); every other value is an error code
kTIMEOUT = -993   # kERROR_TIMEOUT: GoSystem_ReceiveData got nothing in time
kERROR_STATE = -1000   # operation not possible in the sensor's current state

# GoSdkDef.h
GO_TRIGGER_TIME, GO_TRIGGER_INPUT, GO_TRIGGER_SOFTWARE = 0, 2, 3
GO_MODE_SURFACE = 3
GO_FAMILY_2000 = 1   # 2x00 line profilers
GO_SURFACE_GENERATION_TYPE_FIXED_LENGTH = 1
GO_SURFACE_GENERATION_START_TRIGGER_SOFTWARE = 2
GO_DATA_MESSAGE_TYPE_UNIFORM_SURFACE = 8
GO_DATA_MESSAGE_TYPE_SURFACE_INTENSITY = 9
INVALID_RANGE_16BIT = -32768   # height samples with no data

# GoState (GoSdkDef.h)
GO_STATES = {
    0: "online", 1: "offline", 2: "resetting", 3: "connected", 4: "incompatible",
    5: "inconsistent", 6: "unresponsive", 7: "cancelled", 8: "incomplete", 9: "busy",
    10: "ready", 11: "running",
}


class kIpAddress(ctypes.Structure):
    """kIpAddress (kApi/Io/kNetwork.h)."""
    _fields_ = [("version", ctypes.c_int32), ("address", ctypes.c_ubyte * 16)]


def _find_sdk(configured: str) -> Path:
    candidates = ([Path(configured)] if configured else []) + [p for p in SDK_CANDIDATES if p is not None]
    for path in candidates:
        if (path / "GoSdk.dll").is_file() and (path / "kApi.dll").is_file():
            return path
    raise RuntimeError(
        "Gocator SDK not found. Set camera.sdk_path or GOCATOR_SDK to GO_SDK/bin/win64 "
        f"(tried {[str(p) for p in candidates]})"
    )


class _GoSdk:
    """The handful of kApi / GoSdk functions this driver uses, with their C signatures."""

    def __init__(self, sdk_dir: Path):
        # GoSdk.dll pulls in kApi.dll and the VC++ runtime from the same folder
        self._dll_dir = os.add_dll_directory(str(sdk_dir))
        self.kapi = ctypes.CDLL(str(sdk_dir / "kApi.dll"))
        self.go = ctypes.CDLL(str(sdk_dir / "GoSdk.dll"))
        p, P = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        status, size = ctypes.c_int32, ctypes.c_size_t
        for lib, name, res, args in (
            (self.kapi, "kApiLib_Construct", status, [P]),
            (self.kapi, "kIpAddress_Parse", status, [ctypes.POINTER(kIpAddress), ctypes.c_char_p]),
            (self.kapi, "kVersion_Format", status, [ctypes.c_uint32, ctypes.c_char_p, size]),
            (self.go, "GoSdk_Construct", status, [P]),
            (self.go, "GoSystem_Construct", status, [P, p]),
            (self.go, "GoSystem_FindSensorById", status, [p, ctypes.c_uint32, P]),
            (self.go, "GoSystem_FindSensorByIpAddress", status, [p, ctypes.POINTER(kIpAddress), P]),
            (self.go, "GoSensor_Connect", status, [p]),
            (self.go, "GoSensor_Disconnect", status, [p]),
            (self.go, "GoSensor_Id", ctypes.c_uint32, [p]),
            (self.go, "GoSensor_PartNumber", status, [p, ctypes.c_char_p, size]),
            (self.go, "GoSensor_FirmwareVersion", ctypes.c_uint32, [p]),
            (self.go, "GoSensor_State", ctypes.c_int32, [p]),
            (self.go, "GoSensor_Family", ctypes.c_int32, [p]),
            (self.go, "GoSensor_Setup", p, [p]),
            (self.go, "GoSensor_Transform", p, [p]),
            (self.go, "GoSensor_Trigger", status, [p]),
            (self.go, "GoTransform_SetSpeed", status, [p, ctypes.c_double]),
            (self.go, "GoTransform_Speed", ctypes.c_double, [p]),
            (self.go, "GoSetup_FrameRate", ctypes.c_double, [p]),
            (self.go, "GoSetup_SetTriggerSource", status, [p, ctypes.c_int32]),
            (self.go, "GoSetup_TriggerSource", ctypes.c_int32, [p]),
            (self.go, "GoSetup_SetScanMode", status, [p, ctypes.c_int32]),
            (self.go, "GoSetup_SurfaceGeneration", p, [p]),
            (self.go, "GoSurfaceGeneration_SetGenerationType", status, [p, ctypes.c_int32]),
            (self.go, "GoSurfaceGenerationFixedLength_SetStartTrigger", status, [p, ctypes.c_int32]),
            (self.go, "GoSurfaceGenerationFixedLength_SetLength", status, [p, ctypes.c_double]),
            (self.go, "GoSurfaceGenerationFixedLength_Length", ctypes.c_double, [p]),
            (self.go, "GoSystem_EnableData", status, [p, ctypes.c_int32]),
            (self.go, "GoSystem_ClearData", status, [p]),
            (self.go, "GoSystem_Start", status, [p]),
            (self.go, "GoSystem_Stop", status, [p]),
            (self.go, "GoSystem_ReceiveData", status, [p, P, ctypes.c_uint64]),
            (self.go, "GoDataSet_Count", size, [p]),
            (self.go, "GoDataSet_At", p, [p, size]),
            (self.go, "GoDataMsg_Type", ctypes.c_int32, [p]),
            (self.go, "GoSurfaceMsg_Length", size, [p]),
            (self.go, "GoSurfaceMsg_Width", size, [p]),
            (self.go, "GoSurfaceMsg_XResolution", ctypes.c_uint32, [p]),
            (self.go, "GoSurfaceMsg_YResolution", ctypes.c_uint32, [p]),
            (self.go, "GoSurfaceMsg_ZResolution", ctypes.c_uint32, [p]),
            (self.go, "GoSurfaceMsg_XOffset", ctypes.c_int32, [p]),
            (self.go, "GoSurfaceMsg_YOffset", ctypes.c_int32, [p]),
            (self.go, "GoSurfaceMsg_ZOffset", ctypes.c_int32, [p]),
            (self.go, "GoSurfaceMsg_RowAt", ctypes.POINTER(ctypes.c_int16), [p, size]),
            (self.go, "GoSurfaceIntensityMsg_Length", size, [p]),
            (self.go, "GoSurfaceIntensityMsg_Width", size, [p]),
            (self.go, "GoSurfaceIntensityMsg_RowAt", ctypes.POINTER(ctypes.c_uint8), [p, size]),
            (self.go, "GoDestroy", status, [p]),
        ):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = res, args
            setattr(self, name, fn)

    def check(self, status: int, what: str) -> None:
        if status != kOK:
            raise RuntimeError(f"{what} failed (kStatus {status})")


def _rows(row_at, msg, length: int, width: int, dtype) -> np.ndarray:
    """Copy a (length, width) SDK array out row by row (rows are not promised contiguous)."""
    out = np.empty((length, width), dtype=dtype)
    for r in range(length):
        out[r] = np.ctypeslib.as_array(row_at(msg, r), shape=(width,))
    return out


class GocatorCamera(CameraHeightMap):
    """
    Gocator snapshot sensor or line profiler, compatible with the CameraHeightMap interface.

    Usage:
        cam = GocatorCamera(service_config)   # the service section (camera, trigger, ...)
        cam.connect_to_camera()    # finds the sensor by serial (or IP), connects, starts it
        height = cam.capture_image()   # uint16 height map (see capture_image)
        cam.disconnect_camera()

    The sensor's job (set up in its web page) must be in Surface mode with the surface
    (and optionally intensity) sent on Ethernet. ``trigger.trigger_type: software``:
      - snapshot sensor (3x00): sets the job's trigger source to Software.
      - line profiler (2x00): keeps the job's profile trigger (time or encoder) and sets
        surface generation to Fixed Length. Such that one trigger scans ``camera.scan_length_mm`` of belt from that moment.

    ``hardware`` leaves the job as it is and returns the next surface the sensor sends.
    """

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.sdk: Optional[_GoSdk] = None
        self._api_lib = ctypes.c_void_p()
        self._api = ctypes.c_void_p()
        self._system = ctypes.c_void_p()
        self._sensor = ctypes.c_void_p()
        self.serial: Optional[int] = None
        self.part_number = ""
        self.firmware = ""
        self.line_profiler = False
        self._running = False
        self.last_height_mm: Optional[np.ndarray] = None   # float32 mm, NaN = no data
        self.last_intensity: Optional[np.ndarray] = None   # 8-bit, same grid as the height map
        self.last_geometry: dict = {}   # x/y resolution (mm per px) and offsets (mm) of the last frame

    # -- connection lifecycle ------------------------------------------------

    def connect_to_camera(self, timeout: Optional[float] = None) -> GocatorCamera:
        camera = self.camera_config or {}
        serial = str(camera.get("serial_number") or "").strip()
        ip = str(camera.get("camera_ip") or "").strip()
        if not serial and not ip:
            raise RuntimeError("Gocator not specified. Set camera.serial_number or camera.camera_ip in config.")

        sdk_dir = _find_sdk(str(camera.get("sdk_path") or "").strip())
        logger.info("Loading Gocator SDK from %s", sdk_dir)
        self.sdk = sdk = _GoSdk(sdk_dir)
        try:
            sdk.check(sdk.kApiLib_Construct(ctypes.byref(self._api_lib)), "kApiLib_Construct")
            sdk.check(sdk.GoSdk_Construct(ctypes.byref(self._api)), "GoSdk_Construct")
            # constructing the system runs sensor discovery on the network
            sdk.check(sdk.GoSystem_Construct(ctypes.byref(self._system), None), "GoSystem_Construct")

            if serial:
                status = sdk.GoSystem_FindSensorById(self._system, int(serial), ctypes.byref(self._sensor))
                sdk.check(status, f"Gocator with serial_number={serial} not found; GoSystem_FindSensorById")
            else:
                address = kIpAddress()
                sdk.check(sdk.kIpAddress_Parse(ctypes.byref(address), ip.encode()), f"kIpAddress_Parse({ip})")
                status = sdk.GoSystem_FindSensorByIpAddress(self._system, ctypes.byref(address),
                                                            ctypes.byref(self._sensor))
                sdk.check(status, f"Gocator at camera_ip={ip} not found; GoSystem_FindSensorByIpAddress")

            sdk.check(sdk.GoSensor_Connect(self._sensor), "GoSensor_Connect")

            self.serial = int(sdk.GoSensor_Id(self._sensor))
            text = ctypes.create_string_buffer(64)
            if sdk.GoSensor_PartNumber(self._sensor, text, len(text)) == kOK:
                self.part_number = text.value.decode(errors="replace")
            if sdk.kVersion_Format(sdk.GoSensor_FirmwareVersion(self._sensor), text, len(text)) == kOK:
                self.firmware = text.value.decode(errors="replace")
            state = GO_STATES.get(int(sdk.GoSensor_State(self._sensor)), "unknown")
            logger.info("Gocator connected: %s serial=%s firmware=%s state=%s",
                        self.part_number, self.serial, self.firmware, state)

            trigger_cfg = self.trigger_config or {}
            self.trigger_type = str(trigger_cfg.get("trigger_type") or "software").strip().lower()
            if self.trigger_type not in ("software", "hardware"):
                raise RuntimeError(f"Gocator trigger_type must be software or hardware (got {self.trigger_type!r})")
            # settings can only change while stopped (it may be running from the web page)
            sdk.GoSystem_Stop(self._system)
            setup = sdk.GoSensor_Setup(self._sensor)
            # height maps only come in Surface mode (the web page may have left it in Video)
            sdk.check(sdk.GoSetup_SetScanMode(setup, GO_MODE_SURFACE), "GoSetup_SetScanMode(surface)")
            self.line_profiler = int(sdk.GoSensor_Family(self._sensor)) == GO_FAMILY_2000
            if self.line_profiler:
                self._travel_speed(setup, camera.get("travel_speed_mm_s"))
            if self.trigger_type == "software" and self.line_profiler:
                # start a fixed-length surface scan, paced by the job's trigger.
                self._software_started_surface(setup, camera.get("scan_length_mm"))
            elif self.trigger_type == "software":
                sdk.check(sdk.GoSetup_SetTriggerSource(setup, GO_TRIGGER_SOFTWARE), "GoSetup_SetTriggerSource(software)")
            logger.info("Gocator trigger source: %s", int(sdk.GoSetup_TriggerSource(setup)))

            sdk.check(sdk.GoSystem_EnableData(self._system, 1), "GoSystem_EnableData")
            status = sdk.GoSystem_Start(self._system)
            if status == kERROR_STATE:
                # still running from an earlier client (or the web page): stop it, start again
                logger.info("Gocator not startable (state %s); stopping and starting again",
                            GO_STATES.get(int(sdk.GoSensor_State(self._sensor)), "unknown"))
                sdk.GoSystem_Stop(self._system)
                status = sdk.GoSystem_Start(self._system)
            sdk.check(status, "GoSystem_Start")
            self._running = True
        except Exception:
            self.disconnect_camera()
            raise

        self.cam = self._sensor
        return self

    def _travel_speed(self, setup, speed_mm_s) -> None:
        """Line profile (time-triggered): Y spacing = travel speed / frame rate.
        Configure the measured/given line speed and able that assumption to the line cam."""
        sdk = self.sdk
        transform = sdk.GoSensor_Transform(self._sensor)
        if speed_mm_s not in (None, ""):
            sdk.check(sdk.GoTransform_SetSpeed(transform, float(speed_mm_s)),
                      f"GoTransform_SetSpeed({speed_mm_s})")
        speed, rate = sdk.GoTransform_Speed(transform), sdk.GoSetup_FrameRate(setup)
        logger.info("Gocator travel speed %.1f mm/s at %.1f Hz: %.4f mm per profile (time trigger)",
                    speed, rate, speed / rate if rate else float("nan"))

    def _software_started_surface(self, setup, length_mm) -> None:
        """Line profiler: one software trigger -> one fixed-length surface."""
        sdk = self.sdk
        generation = sdk.GoSetup_SurfaceGeneration(setup)
        sdk.check(sdk.GoSurfaceGeneration_SetGenerationType(generation, GO_SURFACE_GENERATION_TYPE_FIXED_LENGTH),
                  "GoSurfaceGeneration_SetGenerationType(fixed length)")
        sdk.check(sdk.GoSurfaceGenerationFixedLength_SetStartTrigger(generation, GO_SURFACE_GENERATION_START_TRIGGER_SOFTWARE),
                  "GoSurfaceGenerationFixedLength_SetStartTrigger(software)")
        if length_mm not in (None, ""):
            sdk.check(sdk.GoSurfaceGenerationFixedLength_SetLength(generation, float(length_mm)),
                      f"GoSurfaceGenerationFixedLength_SetLength({length_mm})")
        logger.info("Gocator line profiler: %.1f mm fixed-length surface per software trigger",
                    sdk.GoSurfaceGenerationFixedLength_Length(generation))

    def capture_image(self, timeout_ms: Optional[float] = None) -> np.ndarray:
        """Take one snapshot, or scan one fixed length on a line profiler and return its height map. 
        On a line profiler the call lasts as long as the scan: scan_length_mm / belt speed (100 mm at 100 mm/s: about 1 s).

        Returns a (rows, cols) uint16 image, one height per pixel:
            value = round(height_mm * 100) + 32768   (0.01 mm steps)
            value = 0                                (no data at that pixel)

        This is published via camera-service and archiving it as a lossless 
        16-bit PNG. The float32 mm map can be recovered (to within 0.005 mm) with:
            height_mm = (value - 32768) / 100        (value = 0 -> NaN)
        which is what archive_functions.decode_raw_height_png does.


        Each call also stores two more images of the same snapshot on the camera object:
            self.last_height_mm   the height map before encoding: float32, in mm,
                                  NaN where there is no data
            self.last_intensity   the grey-level image (uint8), same size as the height
                                  map; None unless intensity output is on in the job
        """
        sdk = self.sdk
        if sdk is None or not self._running:
            raise RuntimeError("Gocator is not connected")
        timeout_s = 10.0 if timeout_ms is None else float(timeout_ms) / 1000.0

        if self.trigger_type == "software":
            sdk.GoSystem_ClearData(self._system)   # drop anything left from an earlier frame
            sdk.check(sdk.GoSensor_Trigger(self._sensor), "GoSensor_Trigger")
        self.last_height_mm = self._receive_surface(timeout_s)
        return prepare_raw_png(self.last_height_mm)

    def _receive_surface(self, timeout_s: float) -> np.ndarray:
        """Receive data sets until one holds a surface; return it as a height map in mm."""
        sdk = self.sdk
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("No Gocator surface received before timeout (job in Surface "
                                   "mode with surface output on Ethernet?)")
            dataset = ctypes.c_void_p()
            status = sdk.GoSystem_ReceiveData(self._system, ctypes.byref(dataset), int(remaining * 1e6))
            if status == kTIMEOUT:
                continue
            sdk.check(status, "GoSystem_ReceiveData")
            try:
                height, intensity = None, None
                for i in range(int(sdk.GoDataSet_Count(dataset))):
                    msg = sdk.GoDataSet_At(dataset, i)
                    kind = int(sdk.GoDataMsg_Type(msg))
                    if kind == GO_DATA_MESSAGE_TYPE_UNIFORM_SURFACE:
                        height = self._height_mm(msg)
                    elif kind == GO_DATA_MESSAGE_TYPE_SURFACE_INTENSITY:
                        length, width = int(sdk.GoSurfaceIntensityMsg_Length(msg)), int(sdk.GoSurfaceIntensityMsg_Width(msg))
                        intensity = _rows(sdk.GoSurfaceIntensityMsg_RowAt, msg, length, width, np.uint8)
            finally:
                sdk.GoDestroy(dataset)
            if height is not None:
                self.last_intensity = intensity
                logger.info("Gocator surface %dx%d received", height.shape[1], height.shape[0])
                return height

    def _height_mm(self, msg) -> np.ndarray:
        """Uniform surface message -> float32 mm, NaN for no data (as in LMI's ReceiveSurface sample)."""
        sdk = self.sdk
        length, width = int(sdk.GoSurfaceMsg_Length(msg)), int(sdk.GoSurfaceMsg_Width(msg))
        raw = _rows(sdk.GoSurfaceMsg_RowAt, msg, length, width, np.int16)
        z_res = sdk.GoSurfaceMsg_ZResolution(msg) / 1e6    # nm -> mm
        z_off = sdk.GoSurfaceMsg_ZOffset(msg) / 1e3         # um -> mm
        geometry = {
            "x_resolution_mm": sdk.GoSurfaceMsg_XResolution(msg) / 1e6,
            "y_resolution_mm": sdk.GoSurfaceMsg_YResolution(msg) / 1e6,
            "z_resolution_mm": z_res,
            "x_offset_mm": sdk.GoSurfaceMsg_XOffset(msg) / 1e3,
            "y_offset_mm": sdk.GoSurfaceMsg_YOffset(msg) / 1e3,
            "z_offset_mm": z_off,
        }
        # pixel size is fixed by the job; CaptureState puts it in the packet ("geometry"). Log it when it changes
        if geometry != self.last_geometry:
            logger.info("Gocator surface geometry: %s",
                        {k: round(v, 4) for k, v in geometry.items()})
        self.last_geometry = geometry
        height = (z_off + z_res * raw.astype(np.float32)).astype(np.float32)
        height[raw == INVALID_RANGE_16BIT] = np.nan
        return height

    def disconnect_camera(self, camera=None) -> None:
        """Disconnect and free the SDK objects. Idempotent; ``camera`` is accepted but ignored."""
        sdk = self.sdk
        if sdk is None:
            return
        if self._running:
            try:
                sdk.GoSystem_Stop(self._system)
            except Exception:
                logger.debug("GoSystem_Stop failed", exc_info=True)
            self._running = False
        if self._sensor:
            try:
                sdk.GoSensor_Disconnect(self._sensor)
            except Exception:
                logger.debug("GoSensor_Disconnect failed", exc_info=True)
        # the sensor belongs to the system: destroying the system frees it. The kApiLib
        # assembly is left alone, as in LMI's samples (GoSdk holds a reference to it).
        for handle in (self._system, self._api):
            if handle:
                try:
                    sdk.GoDestroy(handle)
                except Exception:
                    logger.debug("GoDestroy failed", exc_info=True)
        self._sensor = ctypes.c_void_p()
        self._system = ctypes.c_void_p()
        self._api = ctypes.c_void_p()
        self._api_lib = ctypes.c_void_p()
        self.cam = None
        self.sdk = None
        logger.info("Gocator disconnected")
