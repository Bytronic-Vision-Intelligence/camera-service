from dependencies.CameraLibrary.cameras import Camera
from dependencies.CameraLibrary.hardware_trigger import (
    HardwareTriggerConfig,
    is_camera_loss_error,
    report_camera_loss,
    wait_for_gpio_edge_frames,
)
from dependencies.CameraLibrary.spinnaker_trigger import SpinnakerHardwareTrigger
import PySpin
import logging
import time
from queue import Queue
from threading import Event

import numpy as np


logger = logging.getLogger(__name__)

# Per-model GenICam defaults. AX5 needs CMOSBitDepth + Mono14 (and Mono16 MSB mask);
# A70 exposes Mono16 only, uses IRFormat for temp-linear, and must not strip high bits.
_FLIR_MODEL_PROFILES = {
    "a70": {
        "default_pixel_format": "Mono16",
        "pixel_format_fallbacks": ("Mono16", "Mono8"),
        "set_cmos_bit_depth": False,
        "mask_mono16_msbs": False,
        # A70: temperature linear via IRFormat (not TemperatureLinearMode).
        "temperature_linear_ir_format": "TemperatureLinear10mK",
    },
    "ax5": {
        "default_pixel_format": "Mono14",
        "pixel_format_fallbacks": ("Mono14", "Mono16", "Mono8"),
        "set_cmos_bit_depth": True,
        "default_cmos_bit_depth": "bit14bit",
        "mask_mono16_msbs": True,
        "temperature_linear_ir_format": None,
    },
}


def flir_model_profile(camera_model) -> dict:
    """Return the GenICam profile for ``camera_model`` (``a70`` or ``ax5``)."""
    model = str(camera_model or "ax5").strip().lower()
    try:
        return dict(_FLIR_MODEL_PROFILES[model])
    except KeyError as exc:
        raise ValueError(
            f"Unsupported FLIR camera_model={camera_model!r}; expected one of: a70, ax5"
        ) from exc


def _set_enum_node(nodemap, node_name: str, entry_name: str) -> bool:
    """Set a GenICam enumeration node if available/writable. Returns True on success."""
    try:
        node = PySpin.CEnumerationPtr(nodemap.GetNode(node_name))
        if not PySpin.IsAvailable(node) or not PySpin.IsWritable(node):
            logger.warning("Node %s is not writable", node_name)
            return False
        entry = PySpin.CEnumEntryPtr(node.GetEntryByName(entry_name))
        if not PySpin.IsAvailable(entry) or not PySpin.IsReadable(entry):
            logger.warning("Node %s entry %s is unavailable", node_name, entry_name)
            return False
        node.SetIntValue(entry.GetValue())
        logger.info("%s set to %s", node_name, entry_name)
        return True
    except Exception:
        logger.warning("Failed setting %s=%s", node_name, entry_name, exc_info=True)
        return False


def _set_bool_node(nodemap, node_name: str, value: bool) -> bool:
    try:
        node = PySpin.CBooleanPtr(nodemap.GetNode(node_name))
        if not PySpin.IsAvailable(node) or not PySpin.IsWritable(node):
            logger.warning("Node %s is not writable", node_name)
            return False
        node.SetValue(bool(value))
        logger.info("%s set to %s", node_name, value)
        return True
    except Exception:
        logger.warning("Failed setting %s=%s", node_name, value, exc_info=True)
        return False


class FlirCamera(Camera):
    def __init__(self):
        super().__init__()
        self.cam = None
        self.system = None
        self.cam_list = None
        self.processor = None
        self.pixel_format_out = None
        self._trigger: SpinnakerHardwareTrigger | None = None
        self._mask_mono16_msbs = False
        self._flir_model = "ax5"

    def _configure_raw_pixel_format(self, nodemap) -> None:
        """Apply model-specific pixel format / bit-depth options.

        ``camera_settings.camera_model``:
        - ``ax5``: CMOSBitDepth + Mono14; ``TemperatureLinearMode`` when enabled.
        - ``a70``: Mono16; ``IRFormat=TemperatureLinear10mK`` when temp-linear enabled.
        """
        try:
            cfg = self.camera_settings or {}
        except Exception:
            cfg = {}

        model = str(cfg.get("camera_model") or "ax5").strip().lower()
        try:
            profile = flir_model_profile(model)
        except ValueError:
            logger.warning(
                "Invalid FLIR camera_model=%r; falling back to ax5",
                cfg.get("camera_model"),
            )
            model = "ax5"
            profile = flir_model_profile(model)
        self._flir_model = model

        pixel_format = str(
            cfg.get("pixel_format") or profile["default_pixel_format"]
        )
        temp_linear = str(cfg.get("temperature_linear_mode", "false")).lower()

        logger.info(
            "Configuring FLIR model=%s pixel_format=%s temp_linear=%s",
            self._flir_model,
            pixel_format,
            temp_linear,
        )

        if profile["set_cmos_bit_depth"]:
            cmos_depth = str(
                cfg.get("cmos_bit_depth")
                or profile.get("default_cmos_bit_depth")
                or "bit14bit"
            )
            # CMOS bit depth must match PixelFormat when switching 8 <-> 14 bit.
            _set_enum_node(nodemap, "CMOSBitDepth", cmos_depth)

        if not _set_enum_node(nodemap, "PixelFormat", pixel_format):
            for fallback in profile["pixel_format_fallbacks"]:
                if fallback == pixel_format:
                    continue
                if _set_enum_node(nodemap, "PixelFormat", fallback):
                    pixel_format = fallback
                    break

        # AX5 Mono16 packs a 14-bit payload with bits 14–15 stuck high.
        self._mask_mono16_msbs = bool(
            profile["mask_mono16_msbs"] and pixel_format == "Mono16"
        )

        if temp_linear in ("true", "1", "yes", "on"):
            ir_format = cfg.get("ir_format") or profile.get(
                "temperature_linear_ir_format"
            )
            if ir_format:
                # A70 / A50 family: IRFormat enum (e.g. TemperatureLinear10mK).
                ir_name = str(ir_format)
                if not _set_enum_node(nodemap, "IRFormat", ir_name):
                    # Some firmwares use a spaced symbolic name.
                    alt = ir_name.replace("10mK", " 10mK").replace("100mK", " 100mK")
                    if alt != ir_name:
                        _set_enum_node(nodemap, "IRFormat", alt)
            else:
                # AX5 family: boolean TemperatureLinearMode.
                _set_bool_node(nodemap, "TemperatureLinearMode", True)

        try:
            pf = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
            if PySpin.IsAvailable(pf) and PySpin.IsReadable(pf):
                cur = PySpin.CEnumEntryPtr(pf.GetCurrentEntry())
                if PySpin.IsAvailable(cur) and PySpin.IsReadable(cur):
                    logger.info("Active PixelFormat: %s", cur.GetSymbolic())
        except Exception:
            logger.debug(
                "Unable to read/log active PixelFormat; continuing initialization",
                exc_info=True,
            )

    def _find_camera(self):
        self.cam = None

        try:
            serial = str(self.camera_config.get("serial_number") or "").strip()
        except Exception:
            serial = ""

        if not serial:
            raise RuntimeError("serial_number is required in config.yaml for FLIR cameras")

        self.system = PySpin.System.GetInstance()
        self.cam_list = self.system.GetCameras()
        count = self.cam_list.GetSize()
        if count == 0:
            self.cam_list.Clear()
            self.system.ReleaseInstance()
            self.cam_list = None
            self.system = None
            raise RuntimeError("No FLIR cameras detected")

        try:
            self.cam = self.cam_list.GetBySerial(serial)
        except Exception as e:
            raise RuntimeError(
                f"FLIR camera with serial_number={serial} not found "
                f"(detected={count})"
            ) from e

        if self.cam is None or not self.cam.IsValid():
            raise RuntimeError(
                f"FLIR camera with serial_number={serial} not found "
                f"(detected={count})"
            )
        logger.info("FLIR camera found (serial=%s)", serial)
        return self.cam

    def connect_to_camera(self, timeout_ms: int = 5000):
        timeout_s = timeout_ms / 1000.0
        start = time.time()

        self.cam = self._find_camera()

        try:
            self.cam.Init()
            nodemap = self.cam.GetNodeMap()

            try:
                model = PySpin.CStringPtr(nodemap.GetNode("DeviceModelName"))
                if PySpin.IsAvailable(model) and PySpin.IsReadable(model):
                    logger.info("Camera model: %s", model.GetValue())
            except Exception as e:
                logger.debug(
                    "Unable to read FLIR DeviceModelName; continuing initialization: %s",
                    e,
                )

            acquisition_mode = PySpin.CEnumerationPtr(nodemap.GetNode("AcquisitionMode"))
            if PySpin.IsAvailable(acquisition_mode) and PySpin.IsWritable(acquisition_mode):
                continuous = PySpin.CEnumEntryPtr(acquisition_mode.GetEntryByName("Continuous"))
                if PySpin.IsAvailable(continuous) and PySpin.IsReadable(continuous):
                    acquisition_mode.SetIntValue(continuous.GetValue())

            self._configure_raw_pixel_format(nodemap)

            trigger_cfg = HardwareTriggerConfig.from_app_config(self.trigger_config)
            self._trigger = SpinnakerHardwareTrigger(self.cam, trigger_cfg)
            self._trigger.configure(nodemap)

            self.processor = PySpin.ImageProcessor()
            self.processor.SetColorProcessing(
                PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR
            )
            self.pixel_format_out = PySpin.PixelFormat_BGR8

            self.cam.BeginAcquisition()

            while True:
                if self.cam.IsStreaming():
                    break
                if (time.time() - start) > timeout_s:
                    raise TimeoutError("Timeout while waiting for FLIR acquisition to start.")
                time.sleep(0.05)

            if self._trigger.uses_gpio_poll:
                self._trigger.sample_idle_line()

            logger.info("FLIR camera connected successfully")
            return self.cam
        except Exception as e:
            self.disconnect_camera(self.cam)
            raise RuntimeError("Failed to connect FLIR camera.") from e

    def stop_acquisition(self) -> None:
        if self.cam is None:
            return
        try:
            if self.cam.IsStreaming():
                self.cam.EndAcquisition()
                logger.info("Acquisition stopped")
        except Exception:
            logger.debug("stop_acquisition failed", exc_info=True)

    def capture_image(self, camera=None, timeout_ms: int = 5000, is_converted: bool = False) -> np.ndarray:
        if camera is None:
            camera = self.cam
        if camera is None:
            raise ValueError("camera is None")

        try:
            grab_result = camera.GetNextImage(timeout_ms)
        except PySpin.SpinnakerException as e:
            native_hw = (
                self._trigger is not None
                and self._trigger.mode == "native"
                and "timeout" in str(e).lower()
            )
            if native_hw:
                raise TimeoutError("No hardware trigger within grab timeout") from e
            logger.error("GetNextImage raised an exception", exc_info=True)
            raise RuntimeError("Failed to grab image from FLIR camera") from e
        except Exception as e:
            logger.error("GetNextImage raised an exception", exc_info=True)
            raise RuntimeError("Failed to grab image from FLIR camera") from e

        try:
            if grab_result.IsIncomplete():
                raise RuntimeError(f"Incomplete frame, status={grab_result.GetImageStatus()}")

            if is_converted:
                converted = self.processor.Convert(grab_result, self.pixel_format_out)
                img = converted.GetNDArray()
            else:
                # Native sensor buffer (Mono14/Mono16 → HxW uint16), not BGR8.
                img = grab_result.GetNDArray()
                if self._mask_mono16_msbs and getattr(img, "dtype", None) == np.uint16:
                    # AX5 Mono16 only: bits 14–15 are always 1; keep 14-bit payload.
                    # A70 Mono16 must not be masked (corrupts radiometric / temp-linear DNs).
                    img = np.asarray(img) & np.uint16(0x3FFF)

            logging.info(
                "Captured image shape: %s dtype=%s min=%s max=%s bpp=%s pf=%s converted=%s",
                getattr(img, "shape", None),
                getattr(img, "dtype", None),
                int(np.min(img)) if img is not None else None,
                int(np.max(img)) if img is not None else None,
                grab_result.GetBitsPerPixel(),
                grab_result.GetPixelFormatName(),
                is_converted,
            )
            return np.asarray(img)
        finally:
            try:
                grab_result.Release()
            except Exception:
                logger.error("Failed to release FLIR image buffer", exc_info=True)

    def wait_for_frame(
        self,
        queue: Queue,
        stop_event: Event,
        camera=None,
        timeout_ms: int = 5000,
        is_converted: bool = False,
    ):
        if camera is None:
            camera = self.cam
        if camera is None:
            raise ValueError("camera is None")

        if self._trigger is not None and self._trigger.uses_gpio_poll:
            wait_for_gpio_edge_frames(
                read_line=self._trigger.read_line,
                capture_frame=lambda: self.capture_image(
                    camera=camera,
                    timeout_ms=timeout_ms,
                    is_converted=is_converted,
                ),
                queue=queue,
                stop_event=stop_event,
                config=self._trigger.config,
                initial_status=self._trigger.last_line_status,
            )
            return

        while not stop_event.is_set():
            try:
                frame = self.capture_image(
                    camera=camera,
                    timeout_ms=timeout_ms,
                    is_converted=is_converted,
                )
                queue.put(frame)
            except TimeoutError:
                continue
            except Exception as e:
                if stop_event.is_set():
                    break
                if is_camera_loss_error(e):
                    report_camera_loss(e, queue=queue)
                    break
                logger.error("Failed to retrieve FLIR frame: %s", e, exc_info=True)

    def disconnect_camera(self, camera=None) -> None:
        if camera is None:
            camera = self.cam

        if camera is not None:
            try:
                if camera.IsStreaming():
                    camera.EndAcquisition()
            except Exception:
                logger.debug("EndAcquisition failed during shutdown", exc_info=True)
            try:
                if self._trigger is not None:
                    self._trigger.reset(camera.GetNodeMap())
            except Exception:
                logger.debug("Failed resetting hardware trigger during shutdown", exc_info=True)
            try:
                if camera.IsInitialized():
                    camera.DeInit()
            except Exception:
                logger.debug("DeInit failed during shutdown", exc_info=True)

        self.cam = None
        self._trigger = None
        del camera

        if self.cam_list is not None:
            try:
                self.cam_list.Clear()
            except Exception:
                logger.debug("Failed clearing FLIR camera list", exc_info=True)
            try:
                del self.cam_list
            except Exception:
                logger.debug("Failed deleting FLIR camera list reference", exc_info=True)
            self.cam_list = None

        if self.system is not None:
            try:
                self.system.ReleaseInstance()
            except Exception:
                logger.debug("Failed releasing FLIR system instance", exc_info=True)
            self.system = None

        logger.info("FLIR camera disconnected")
