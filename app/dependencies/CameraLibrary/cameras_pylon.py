from pypylon import pylon
from dependencies.CameraLibrary.cameras import Camera
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
            serial = str(self.camera_config.get("serial_number") or "").strip()
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

    def _apply_camera_settings(self, camera: pylon.InstantCamera) -> None:
        """Apply optional ``camera_settings`` from the nested config (no trigger setup)."""
        """Set as None for now, camera settings to be added when needed"""
        return None

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