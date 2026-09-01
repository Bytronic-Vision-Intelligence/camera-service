from harvesters.core import Harvester
from dependencies.CameraLibrary.cameras import Camera
from dependencies import loadConfig
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
    """Normalize config strings to GenICam PascalCase symbols."""
    s = str(value).strip()
    if not s:
        return s
    if s.islower() or s.isupper():
        return s.capitalize()
    return s


# Config aliases for features whose user-facing names differ from GenICam symbols.
_GENICAM_ENUM_ALIASES = {
    "LineMode": {
        "strobe": "Output",
        "output": "Output",
        "input": "Input",
    },
}


def _set_genicam_enum(node, feature_name: str, value: str) -> str:
    """Set a GenICam enum node, resolving config aliases against valid symbols."""
    desired = str(value).strip()
    aliases = _GENICAM_ENUM_ALIASES.get(feature_name, {})
    resolved = aliases.get(desired.lower(), _genicam_enum_value(desired))

    symbolics = [str(s) for s in getattr(node, "symbolics", [])]
    if symbolics:
        for candidate in (resolved, desired, _genicam_enum_value(desired)):
            for symbolic in symbolics:
                if symbolic == candidate or symbolic.lower() == candidate.lower():
                    node.value = symbolic
                    return symbolic
        raise ValueError(
            f"{feature_name}: no match for {value!r} (resolved={resolved!r}, valid={symbolics})"
        )

    node.value = resolved
    return resolved


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

    def _find_camera(self):
        """Open by ``camera.serial_number`` when set; otherwise first available device."""
        self.cam = None
        self.harvester = None

        try:
            serial = str(loadConfig.return_config_value("camera.serial_number") or "").strip()
        except Exception:
            serial = ""

        try:
            try:
                configured = str(loadConfig.return_config_value("camera.gentl_cti") or "").strip()
            except Exception:
                configured = ""

            cti_candidates = ([Path(configured)] if configured else []) + [
                p for p in CTI_CANDIDATES if p is not None
            ]

            cti = next((p for p in cti_candidates if p.is_file()), None)
            if cti is None:
                raise RuntimeError(
                    "No GenTL .cti found. Set camera.gentl_cti or GIGE_CTI "
                    "(Baumer bgapi2_gige.cti recommended)."
                )

            h = Harvester()
            h.add_file(str(cti))
            h.update()
            devices = h.device_info_list
            if not devices:
                h.reset()
                raise RuntimeError("No GigE cameras detected")

            index = 0
            if serial:
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
                index = matched

            props = devices[index].property_dict
            logging.info(
                "Camera found: %s (serial=%s)",
                props.get("model"),
                props.get("serial_number"),
            )

            cam = h.create(index)
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

    def _apply_camera_settings(self, camera) -> None:
        """Apply optional ``camera_settings`` from the nested config (no trigger setup)."""
        nm = camera.remote_device.node_map
        cfg = loadConfig.get_section("camera_settings")

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

    def _apply_lights_settings(self, camera) -> None:
        """Apply optional ``lights`` config (digital output / strobe line)."""
        cfg = loadConfig.get_section("lights")
        if not cfg:
            return

        line = cfg.get("line")
        if line is None or str(line).strip() == "":
            return

        line_selector = _line_selector_name(line)
        line_mode_cfg = str(cfg.get("line_mode") or "strobe")
        line_source_cfg = str(cfg.get("line_source") or "ExposureActive")

        nm = camera.remote_device.node_map
        try:
            nm.LineSelector.value = line_selector
            line_mode = _set_genicam_enum(nm.LineMode, "LineMode", line_mode_cfg)
            if line_mode.lower() != "input":
                line_source = _set_genicam_enum(
                    nm.LineSource, "LineSource", line_source_cfg
                )
            else:
                line_source = line_source_cfg
        except Exception as e:
            raise RuntimeError(
                f"Failed configuring lights (line={line_selector}, "
                f"mode={line_mode}, source={line_source})"
            ) from e

        logging.info(
            "Lights configured: LineSelector=%s LineMode=%s LineSource=%s",
            line_selector,
            line_mode,
            line_source if line_mode.lower() != "input" else "n/a",
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

            # GigE trigger_type:
            #   hardware    → line trigger (frame thread)
            #   software    → GenICam TriggerSoftware (MQTT)
            #   continuous  → TriggerMode Off; MQTT pulls next ready frame (may be stale)
            trigger_cfg = loadConfig.get_section("trigger")
            trigger_type = str(trigger_cfg.get("trigger_type") or "software").strip().lower()
            if trigger_type not in ("hardware", "software", "continuous"):
                raise RuntimeError(
                    f"GigE trigger_type must be hardware, software, or continuous "
                    f"(got {trigger_type!r})"
                )
            self.trigger_type = trigger_type

            try:
                nm.TriggerSelector.value = "FrameStart"
            except Exception as e:
                # Some cameras do not expose or allow writing TriggerSelector.
                # This is optional here, so continue with trigger setup.
                logging.debug("Skipping TriggerSelector=FrameStart: %s", e)

            if trigger_type == "hardware":
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
                    "TriggerMode=On (hardware): source=%s activation=%s",
                    source,
                    activation,
                )
            elif trigger_type == "software":
                try:
                    nm.TriggerSource.value = "Software"
                    nm.TriggerMode.value = "On"
                except Exception as e:
                    raise RuntimeError("Failed arming software trigger") from e
                logging.info("TriggerMode=On (software): TriggerSource=Software")
            else:
                try:
                    nm.TriggerMode.value = "Off"
                except Exception as e:
                    raise RuntimeError("Failed setting TriggerMode=Off for continuous") from e
                logging.info("TriggerMode=Off (continuous)")

            self.cam.num_buffers = 4
            self.cam.start()

            if trigger_type == "software":
                nm.TriggerSoftware.execute()
                with self.cam.fetch(timeout=timeout_s) as buffer:
                    _ = np.asarray(buffer.payload.components[0].data).copy()
            elif trigger_type == "continuous":
                with self.cam.fetch(timeout=timeout_s) as buffer:
                    _ = np.asarray(buffer.payload.components[0].data).copy()
                with self.cam.fetch(timeout=timeout_s) as buffer:
                    _ = np.asarray(buffer.payload.components[0].data).copy()

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
            if self.trigger_type == "software":
                camera.remote_device.node_map.TriggerSoftware.execute()
            with camera.fetch(timeout=timeout_ms / 1000.0) as buffer:
                component = buffer.payload.components[0]
                raw = np.asarray(component.data)
                if raw.ndim >= 2:
                    img = raw.copy()
                else:
                    img = raw.reshape(int(component.height), int(component.width)).copy()
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
