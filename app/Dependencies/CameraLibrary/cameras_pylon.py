from pypylon import pylon
from Dependencies.CameraLibrary.cameras import Camera
from Dependencies import loadConfig
from queue import Queue
from threading import Event
import logging
import time
import numpy as np

class PylonCamera(Camera):
    def __init__(self):
        super().__init__()

    def _find_camera(self) -> pylon.InstantCamera:
        """Open by ``camera.serial_number`` when set; otherwise first available device."""
        self.cam = None

        try:
            serial = str(loadConfig.return_config_value("camera.serial_number") or "").strip()
        except Exception:
            serial = ""

        try:
            tl_factory = pylon.TlFactory.GetInstance()

            if not serial:
                device = tl_factory.CreateFirstDevice()
                cam = pylon.InstantCamera(device)
                logging.info(
                    "Camera found: %s (first device, serial=%s)",
                    cam.GetDeviceInfo().GetModelName(),
                    cam.GetDeviceInfo().GetSerialNumber(),
                )
                self.cam = cam
                return cam

            devices = tl_factory.EnumerateDevices()
            if not devices:
                raise RuntimeError("No Pylon cameras detected")

            matched = None
            for info in devices:
                if info.GetSerialNumber() == serial:
                    matched = info
                    break

            if matched is None:
                available = [info.GetSerialNumber() for info in devices]
                raise RuntimeError(
                    f"Pylon camera with serial_number={serial} not found "
                    f"(detected={available})"
                )

            cam = pylon.InstantCamera(tl_factory.CreateDevice(matched))
            logging.info(
                "Camera found: %s (serial=%s)",
                cam.GetDeviceInfo().GetModelName(),
                serial,
            )
            self.cam = cam
            return cam
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError("Error finding camera: " + str(e)) from e

    @staticmethod
    def _set_node(node_map, name: str, value) -> None:
        """Set one GenICam node, logging and carrying on when it is unavailable."""
        if value is None:
            return
        try:
            getattr(node_map, name).Value = value
            logging.info("%s -> %s", name, value)
        except Exception as e:
            logging.warning("Could not set %s=%s: %s", name, value, e)

    def _apply_camera_settings(self, camera: pylon.InstantCamera) -> None:
        """Apply GigE stream tuning from ``camera_settings``.

        pylon's defaults assume a gigabit path. This camera sits on a 100 Mb/s
        switch port, where it outruns the wire and every frame arrives
        incomplete (error 0xe1000014), so the throughput limit and the frame
        retention have to match the link the camera is actually on.
        """
        settings = loadConfig.get_section("camera_settings")
        node_map = camera.GetNodeMap()

        # The packet payload must still fit the NIC MTU once UDP/IP/Ethernet
        # headers are added: 1500 - 28 = 1472. Larger is dropped in silence.
        self._set_node(node_map, "GevSCPSPacketSize", settings.get("packet_size", 1472))

        throughput = settings.get("throughput_limit")
        if throughput:
            self._set_node(node_map, "DeviceLinkThroughputLimitMode", "On")
            self._set_node(node_map, "DeviceLinkThroughputLimit", int(throughput))

        buffers = settings.get("buffer_size")
        if buffers:
            try:
                camera.MaxNumBuffer.Value = int(buffers)
            except Exception as e:
                logging.warning("Could not set MaxNumBuffer: %s", e)

        # Host side, not camera side: these live on the stream grabber and are
        # per-application, so setting them on the camera would not carry over.
        stream = camera.GetStreamGrabberNodeMap()
        self._set_node(stream, "FrameRetention", settings.get("frame_retention_ms", 3000))
        self._set_node(stream, "PacketTimeout", settings.get("packet_timeout_ms", 500))

    def connect_to_camera(self, timeout_ms: int = 5000) -> pylon.InstantCamera:
        # Connect to the camera and return the camera object.
        # Function returns the camera object.
        timeout_s = timeout_ms / 1000.0
        start = time.time()

        self.cam = self._find_camera()

        try:
            if not self.cam.IsOpen():
                try:
                    self.cam.Open()
                except Exception as e:
                    # Open may fail transiently; continue to wait until timeout
                    logging.error("Initial Open() failed; entering wait loop")

            while not self.cam.IsOpen():
                if time.time() - start > timeout_s:
                    raise TimeoutError("Timeout while waiting for camera to open.")
                time.sleep(0.1)

            self._apply_camera_settings(self.cam)

            logging.info("Camera connected successfully")
            return self.cam

        except Exception as e:
            # ensure camera is closed on failure
            if self.cam is not None and self.cam.IsOpen():
                self.cam.Close()
            raise RuntimeError("Failed to open camera within timeout.") from e
        
    def capture_image(
            self, 
            camera: pylon.InstantCamera = None, 
            timeout_ms: int = 5000, 
            is_converted=True
            ) -> np.ndarray:
        
        #capture an image from the camera and return it as a numpy array
        #function will return the image as a numpy array
        if camera is None:
            camera = self.cam
        if camera is None:
            raise ValueError("camera is None")
        if not camera.IsOpen():
            raise RuntimeError("camera is not open")

        try:
            grab_result = camera.GrabOne(timeout_ms)  # pylon expects ms
        except Exception as e:
            logging.error("GrabOne raised an exception")
            raise RuntimeError("Failed to grab image") from e

        try:
            if not grab_result.GrabSucceeded():
                error_code = getattr(grab_result, "ErrorCode", None)
                error_desc = getattr(grab_result, "ErrorDescription", None)
                error_info = f"Grab failed with error code {error_code}, description: {error_desc}"
                raise RuntimeError(error_info)

            img = grab_result.Array  # type: ignore

            if is_converted:
                converter = pylon.ImageFormatConverter()
                converter.OutputPixelFormat = pylon.PixelType_BGR8packed
                converted = converter.Convert(grab_result)
                img = converted.Array

            logging.info("Captured image shape: %s", getattr(img, "shape", None))
            return np.asarray(img)

        finally:
            try:
                grab_result.Release()
            except Exception:
                logging.error("Failed to release grab_result", exc_info=True)

    def wait_for_frame(
            self,
            queue: Queue,
            stop_event: Event,
            camera: pylon.InstantCamera = None,
            timeout_ms: int = 5000,
            is_converted: bool = True,
            ):
        """Continuously retrieve frames from the camera and enqueue image arrays."""
        if camera is None:
            camera = self.cam
        if camera is None:
            raise ValueError("camera is None")
        if not camera.IsOpen():
            raise RuntimeError("camera is not open")

        if not camera.IsGrabbing():
            camera.StartGrabbing(pylon.GrabStrategy_OneByOne)

        try:
            while not stop_event.is_set():
                try:
                    grab_result = camera.RetrieveResult(timeout_ms, pylon.TimeoutHandling_ThrowException)
                except Exception as e:
                    logging.error("Failed to retrieve frame: %s", e, exc_info=True)
                    continue

                try:
                    if not grab_result.GrabSucceeded():
                        error_code = getattr(grab_result, "ErrorCode", None)
                        error_desc = getattr(grab_result, "ErrorDescription", None)
                        logging.error("Grab failed with error code %s, description: %s", error_code, error_desc)
                        continue

                    img = grab_result.Array
                    if is_converted:
                        converter = pylon.ImageFormatConverter()
                        converter.OutputPixelFormat = pylon.PixelType_BGR8packed
                        converted = converter.Convert(grab_result)
                        img = converted.Array

                    queue.put(np.asarray(img))
                finally:
                    try:
                        grab_result.Release()
                    except Exception:
                        logging.error("Failed to release grab_result", exc_info=True)
        finally:
            if camera.IsGrabbing():
                camera.StopGrabbing()

    def disconnect_camera(self, camera) -> None:
        #disconnect the camera
        #function will return nothing
        if camera is not None and camera.IsOpen():
            camera.Close()