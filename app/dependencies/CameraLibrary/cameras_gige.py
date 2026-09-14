from harvesters.core import Harvester
from dependencies.CameraLibrary.cameras import Camera
from queue import Queue
from threading import Event
import logging
import time
import os
from pathlib import Path
import cv2
import numpy as np

CTI_CANDIDATES = [
    Path(os.environ["GIGE_CTI"]) if os.environ.get("GIGE_CTI") else None,
    Path("/opt/baumer-gapi-sdk-cpp/lib/libbgapi2_gige.cti"),
    Path("/opt/baumer-gapi-sdk-cpp/lib/libbgapi2_usb.cti"),
    Path(r"C:\Program Files (x86)\Optotune AG\Optotune cockpit\Resources\GenICamCtiFiles\bgapi2_gige.cti"),
    Path(r"C:\Program Files\Baumer\Baumer GAPI SDK\bin\bgapi2_gige.cti"),
    Path(r"C:\Program Files\Lucid Vision Labs\Arena SDK\x64Release\GenTL_LUCID_v140.cti"),
    Path(r"C:\Program Files\Basler\pylon 7\Runtime\x64\ProducerGEV.cti"),
]


def _genicam_enum_value(value: str) -> str:
    """Map config strings like ``output`` to GenICam symbols like ``Output``."""
    s = str(value).strip()
    if not s:
        return s
    if s.islower() or s.isupper():
        return s.capitalize()
    return s


def _resolve_lights_config(cfg: dict) -> tuple[str, str, str | None]:
    """Map ``lights`` config to GenICam line selector, mode, and optional source."""
    line_selector = _line_selector_name(cfg.get("line"))
    mode_cfg = str(cfg.get("line_mode") or "strobe").strip().lower()
    source_cfg = str(cfg.get("line_source") or "").strip()

    if mode_cfg in ("strobe", "output"):
        line_mode = "Output"
        line_source = _genicam_enum_value(source_cfg or "ExposureActive")
    elif mode_cfg == "input":
        line_mode = "Input"
        line_source = None
    else:
        line_mode = _genicam_enum_value(mode_cfg)
        if line_mode.lower() == "input":
            line_source = None
        else:
            line_source = _genicam_enum_value(source_cfg or "ExposureActive")

    return line_selector, line_mode, line_source


def _enum_writable(node) -> bool:
    try:
        return bool(node.is_writable)
    except Exception:
        return False


def _enum_symbolics(node) -> list[str]:
    try:
        return [entry.symbolic for entry in node.symbolics]
    except Exception:
        return []


def _set_enum_value(node, value: str, label: str) -> None:
    """Set an enumeration; tolerate already-correct state."""
    current = str(node.value)
    if current == value:
        logging.info("%s already %s", label, value)
        return
    try:
        node.value = value
    except Exception as exc:
        raise RuntimeError(
            f"{label} not writable (current={current!r}, requested={value!r})"
        ) from exc
    logging.info("%s set to %s", label, value)


def _select_line(nm, selector: str) -> bool:
    """Select a digital IO line without rewriting LineSelector when already active."""
    try:
        if str(nm.LineSelector.value) == selector:
            return True
    except Exception:
        pass

    if not _enum_writable(nm.LineSelector):
        try:
            return str(nm.LineSelector.value) == selector
        except Exception:
            return False

    nm.LineSelector.value = selector
    return True


def _line_selector_name(line) -> str:
    s = str(line).strip()
    if not s:
        return s
    if s.lower().startswith("line"):
        suffix = s[4:]
        return f"Line{suffix}" if suffix.isdigit() else s
    if s.isdigit():
        return f"Line{s}"
    return s


class GigeCamera(Camera):
    def __init__(self):
        super().__init__()
        self.cam = None
        self.harvester = None
        self.pixel_format = None
        self.trigger_type = None
        self._lights_line_selector: str | None = None
        self._lights_line_source: str | None = None

    def _find_camera(self):
        """Open by ``camera.serial_number``; fail if not set or not found."""
        self.cam = None
        self.harvester = None

        try:
            serial = str(self.camera_config.get("serial_number") or "").strip()
        except Exception:
            serial = ""

        if not serial:
            raise RuntimeError(
                "GigE camera not specified. Set camera.serial_number in config."
            )

        try:
            try:
                configured = str(self.camera_config.get("cti_path") or "").strip()
            except Exception:
                configured = ""

            cti_candidates = ([Path(configured)] if configured else []) + [
                p for p in CTI_CANDIDATES if p is not None
            ]

            cti = next((p for p in cti_candidates if p.is_file()), None)
            if cti is None:
                raise RuntimeError(
                    "No GenTL .cti found. Set camera.cti_path or GIGE_CTI "
                    "(Baumer bgapi2_gige.cti recommended)."
                )

            h = Harvester()
            h.add_file(str(cti))
            h.update()
            devices = h.device_info_list
            if not devices:
                h.reset()
                raise RuntimeError("No GigE cameras detected")

            matched = None
            for i, info in enumerate(devices):
                if str(info.property_dict.get("serial_number") or "") == serial:
                    matched = i
                    break
            if matched is None:
                available = [d.property_dict.get("serial_number") for d in devices]
                h.reset()
                raise RuntimeError(
                    f"GigE camera with serial_number={serial} not found "
                    f"(detected={available})"
                )

            props = devices[matched].property_dict
            logging.info(
                "Camera found: %s (serial=%s)",
                props.get("model"),
                props.get("serial_number"),
            )

            cam = h.create(matched)
            self.harvester = h
            self.cam = cam
            return cam
        except RuntimeError:
            raise
        except Exception as e:
            if self.harvester is not None:
                try:
                    self.harvester.reset()
                except Exception:
                    logging.debug("Ignoring harvester reset failure during camera discovery error handling.", exc_info=True)
                self.harvester = None
            raise RuntimeError("Error finding camera: " + str(e)) from e

    def _set_exposure(self, camera, exposure=None) -> None:
        """Set ``ExposureTime`` (us). Defaults to ``camera_settings`` when omitted."""
        if exposure is None:
            cfg = self.camera_settings or {}
            exposure = cfg.get("exposure_time_us", cfg.get("exposure_time"))
        if exposure is None or str(exposure).strip() == "":
            return

        nm = camera.remote_device.node_map
        try:
            try:
                nm.ExposureAuto.value = "Off"
            except Exception:
                logging.debug("ExposureAuto not settable; continuing", exc_info=True)
            try:
                nm.ExposureMode.value = "Timed"
            except Exception:
                logging.debug("ExposureMode not settable; continuing", exc_info=True)
            nm.ExposureTime.value = float(exposure)
            logging.info("ExposureTime set to %s us", nm.ExposureTime.value)
        except Exception as e:
            raise RuntimeError(f"Failed setting ExposureTime={exposure}") from e

    def _set_gain(self, camera, gain=None) -> None:
        """Set ``Gain``. Defaults to ``camera_settings`` when omitted."""
        if gain is None:
            cfg = self.camera_settings or {}
            gain = cfg.get("gain")
        if gain is None or str(gain).strip() == "":
            return

        nm = camera.remote_device.node_map
        try:
            try:
                nm.GainAuto.value = "Off"
            except Exception:
                logging.debug("GainAuto not settable; continuing", exc_info=True)
            nm.Gain.value = float(gain)
            logging.info("Gain set to %s", nm.Gain.value)
        except Exception as e:
            raise RuntimeError(f"Failed setting Gain={gain}") from e

    def _apply_camera_settings(self, camera) -> None:
        """Apply optional ``camera_settings`` from the nested config (no trigger setup)."""
        nm = camera.remote_device.node_map
        cfg = self.camera_settings or {}

        pixel_format = str(cfg.get("pixel_format") or "").strip()
        if pixel_format:
            try:
                nm.PixelFormat.value = pixel_format
                logging.info("PixelFormat set to %s", pixel_format)
            except Exception as e:
                raise RuntimeError(f"Failed setting PixelFormat={pixel_format}") from e

        try:
            self.pixel_format = str(nm.PixelFormat.value)
        except Exception:
            self.pixel_format = pixel_format or None

        self._set_exposure(camera)
        self._set_gain(camera)

    def _apply_lights_settings(self, camera) -> None:
        """Apply optional ``lights`` config (digital output / strobe line)."""
        cfg = self.lights_config or {}
        if not cfg:
            return

        line = cfg.get("line")
        if line is None or str(line).strip() == "":
            return

        line_selector, line_mode, line_source = _resolve_lights_config(cfg)
        nm = camera.remote_device.node_map

        if not _select_line(nm, line_selector):
            raise RuntimeError(f"Could not select {line_selector}")

        try:
            current_mode = str(nm.LineMode.value)
        except Exception as exc:
            raise RuntimeError(f"Could not read LineMode on {line_selector}") from exc

        if line_mode.lower() == "output" and current_mode.lower() == "input":
            raise RuntimeError(
                f"{line_selector} is input-only; use an output line for strobe "
                f"(Cognex CIC strobe is Line0)"
            )

        _set_enum_value(nm.LineMode, line_mode, f"{line_selector} LineMode")

        if line_source is not None:
            current_source = str(nm.LineSource.value)
            if current_source != line_source:
                _set_enum_value(
                    nm.LineSource,
                    line_source,
                    f"{line_selector} LineSource",
                )

        logging.info(
            "Lights configured: LineSelector=%s LineMode=%s LineSource=%s",
            line_selector,
            nm.LineMode.value,
            nm.LineSource.value if line_source is not None else "n/a",
        )
        self._lights_line_selector = line_selector
        self._lights_line_source = line_source

    def _ensure_lights_for_capture(self, camera) -> None:
        """Re-assert strobe LineSource before each capture if the camera reset it."""
        if not self._lights_line_selector or not self._lights_line_source:
            return
        nm = camera.remote_device.node_map
        try:
            if not _select_line(nm, self._lights_line_selector):
                return
            if str(nm.LineSource.value) == self._lights_line_source:
                return
            nm.LineSource.value = self._lights_line_source
            logging.info(
                "Restored %s LineSource to %s before capture",
                self._lights_line_selector,
                self._lights_line_source,
            )
        except Exception as exc:
            logging.debug("Could not restore lights before capture: %s", exc)

    def _try_apply_lights_settings(self, camera) -> None:
        """Apply strobe/line config; log and continue if the camera IO is read-only."""
        try:
            self._apply_lights_settings(camera)
        except Exception as exc:
            logging.warning(
                "Could not apply lights config (%s); continuing with camera defaults",
                exc,
            )

    def connect_to_camera(self, timeout_ms: int = 5000):
        # Connect to the camera and return the camera object.
        # Function returns the camera object.
        timeout_s = timeout_ms / 1000.0
        start = time.time()

        self.cam = self._find_camera()

        try:
            while self.cam is None:
                if time.time() - start > timeout_s:
                    raise TimeoutError("Timeout while waiting for camera to open.")
                time.sleep(0.1)

            nm = self.cam.remote_device.node_map
            # Default packet size on Cognex CIC is often 576 — too small for a full frame.
            for packet_size in (1500, 3000, 8000, 9000):
                try:
                    nm.GevSCPSPacketSize.value = packet_size
                    logging.info("GevSCPSPacketSize=%s", nm.GevSCPSPacketSize.value)
                    break
                except Exception:
                    continue

            self._apply_camera_settings(self.cam)
            self._apply_lights_settings(self.cam)

            # GigE trigger_type + capture_type:
            #   software + single     → GenICam TriggerSoftware (MQTT)
            #   software + continuous → TriggerMode Off; MQTT on/off streaming
            #   hardware + single     → line trigger (frame thread)
            #   hardware + continuous → TriggerMode Off; frame thread streams
            trigger_cfg = self.trigger_config or {}
            trigger_type = str(trigger_cfg.get("trigger_type") or "software").strip().lower()
            capture_type = str(trigger_cfg.get("capture_type") or "single").strip().lower()
            if trigger_type not in ("hardware", "software"):
                raise RuntimeError(
                    f"GigE trigger_type must be hardware or software (got {trigger_type!r})"
                )
            if capture_type not in ("single", "continuous"):
                raise RuntimeError(
                    f"GigE capture_type must be single or continuous (got {capture_type!r})"
                )
            self.trigger_type = trigger_type
            self.capture_type = capture_type

            try:
                nm.TriggerSelector.value = "FrameStart"
            except Exception as e:
                # Some cameras do not expose or allow writing TriggerSelector.
                # This is optional here, so continue with trigger setup.
                logging.debug("Skipping TriggerSelector=FrameStart: %s", e)

            if trigger_type == "hardware" and capture_type == "single":
                source = str(trigger_cfg.get("trigger_source") or "Line1")
                activation = str(trigger_cfg.get("trigger_activation") or "RisingEdge")
                try:
                    nm.TriggerSource.value = source
                    nm.TriggerActivation.value = activation
                    nm.TriggerMode.value = "On"
                except Exception as e:
                    raise RuntimeError(
                        f"Failed arming hardware trigger (source={source}, activation={activation})"
                    ) from e
                logging.info(
                    "TriggerMode=On (hardware/single): source=%s activation=%s",
                    source,
                    activation,
                )
            elif trigger_type == "software" and capture_type == "single":
                try:
                    nm.TriggerSource.value = "Software"
                    nm.TriggerMode.value = "On"
                except Exception as e:
                    raise RuntimeError("Failed arming software trigger") from e
                logging.info("TriggerMode=On (software/single): TriggerSource=Software")
            else:
                try:
                    nm.TriggerMode.value = "Off"
                except Exception as e:
                    raise RuntimeError(
                        "Failed setting TriggerMode=Off for continuous capture"
                    ) from e
                logging.info(
                    "TriggerMode=Off (%s/%s)",
                    trigger_type,
                    capture_type,
                )

            self.cam.num_buffers = 4
            self.cam.start()

            if trigger_type == "software" and capture_type == "single":
                nm.TriggerSoftware.execute()
                with self.cam.fetch(timeout=timeout_s) as buffer:
                    _ = np.asarray(buffer.payload.components[0].data).copy()
            elif capture_type == "continuous":
                with self.cam.fetch(timeout=timeout_s) as buffer:
                    _ = np.asarray(buffer.payload.components[0].data).copy()
                with self.cam.fetch(timeout=timeout_s) as buffer:
                    _ = np.asarray(buffer.payload.components[0].data).copy()

            # Cognex LineSource is writable after the first frame grab.
            self._try_apply_lights_settings(self.cam)

            logging.info("Camera connected successfully")
            return self.cam

        except Exception as e:
            self.disconnect_camera(self.cam)
            raise RuntimeError("Failed to open camera within timeout.") from e

    def capture_image(
            self,
            camera=None,
            timeout_ms: int = 5000,
            is_converted=True
            ) -> np.ndarray:

        #capture an image from the camera and return it as a numpy array
        #function will return the image as a numpy array
        if camera is None:
            camera = self.cam
        if camera is None:
            raise ValueError("camera is None")
        if not camera.is_acquiring():
            camera.num_buffers = 4
            camera.start()

        try:
            self._ensure_lights_for_capture(camera)
            if self.trigger_type == "software":
                camera.remote_device.node_map.TriggerSoftware.execute()
            with camera.fetch(timeout=timeout_ms / 1000.0) as buffer:
                component = buffer.payload.components[0]
                raw = np.asarray(component.data)
                h = int(component.height)
                w = int(component.width)
                if raw.ndim >= 2:
                    img = raw.copy()
                else:
                    # Mono/Bayer: H*W; packed RGB/BGR: H*W*C
                    plane = h * w
                    if raw.size == plane:
                        img = raw.reshape(h, w).copy()
                    elif plane and raw.size % plane == 0:
                        img = raw.reshape(h, w, raw.size // plane).copy()
                    else:
                        raise ValueError(
                            f"Unexpected buffer size {raw.size} for {h}x{w}"
                        )
        except Exception as e:
            logging.error("fetch raised an exception")
            raise RuntimeError("Failed to grab image") from e

        if is_converted and self.pixel_format:
            if "BayerRG" in self.pixel_format:
                img = cv2.cvtColor(img, cv2.COLOR_BayerRG2BGR)
            elif "BayerGB" in self.pixel_format:
                img = cv2.cvtColor(img, cv2.COLOR_BayerGB2BGR)
            elif "BayerGR" in self.pixel_format:
                img = cv2.cvtColor(img, cv2.COLOR_BayerGR2BGR)
            elif "BayerBG" in self.pixel_format:
                img = cv2.cvtColor(img, cv2.COLOR_BayerBG2BGR)
            elif self.pixel_format.startswith("RGB"):
                # Match Bayer path: return BGR for OpenCV / archive consumers
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        logging.info("Captured image shape: %s", getattr(img, "shape", None))
        return np.asarray(img)

    def wait_for_frame(
            self,
            queue: Queue,
            stop_event: Event,
            camera=None,
            timeout_ms: int = 5000,
            is_converted: bool = True,
            ):
        """Continuously retrieve frames from the camera and enqueue image arrays."""
        if camera is None:
            camera = self.cam
        if camera is None:
            raise ValueError("camera is None")
        if not camera.is_acquiring():
            camera.num_buffers = 4
            camera.start()

        try:
            while not stop_event.is_set():
                try:
                    frame = self.capture_image(
                        camera=camera,
                        timeout_ms=timeout_ms,
                        is_converted=is_converted,
                    )
                    queue.put(frame)
                except Exception as e:
                    logging.error("Failed to retrieve frame: %s", e, exc_info=True)
                    continue
        finally:
            if camera is not None and camera.is_acquiring():
                camera.stop()

    def disconnect_camera(self, camera=None) -> None:
        #disconnect the camera
        #function will return nothing
        if camera is None:
            camera = self.cam

        if camera is not None:
            try:
                if camera.is_acquiring():
                    camera.stop()
            except Exception as e:
                logging.warning("Failed to stop camera during disconnect: %s", e, exc_info=True)
            try:
                camera.destroy()
            except Exception as e:
                logging.warning("Failed to destroy camera during disconnect: %s", e, exc_info=True)

        self.cam = None

        if self.harvester is not None:
            try:
                self.harvester.reset()
            except Exception as e:
                logging.warning("Failed to reset harvester during disconnect: %s", e, exc_info=True)
            self.harvester = None

        self.pixel_format = None
        self.trigger_type = None
        self._lights_line_selector = None
        self._lights_line_source = None
