import cv2
import logging
from numpy import ndarray
from queue import Empty, Queue
from threading import Event, Thread
from time import sleep

class Camera:
    def __init__(self, config: dict | None = None):
        """ Loading camera config at camera object creation. """
        config = config or {}
        self.camera = any
        self.cam = self.camera
        self.framerate = 60
        self.frame_pace = 1/self.framerate

        self.camera_config: dict = config.get("camera") or {}
        self.trigger_config: dict = config.get("trigger") or {}
        self.camera_settings: dict = config.get("camera_settings") or {}
        self.lights_config: dict = config.get("lights") or {}
        self.image_outputs = {}

        self.trigger_type = str(self.trigger_config.get("trigger_type") or "").strip().lower()
        self.capture_type = str(self.trigger_config.get("capture_type") or "").strip().lower()

        self.buffer = [0] * self.camera_settings.get("buffer_size", 0)
        self.buffer_position = 0

        # Created and started by CameraSM after connect_to_camera().
        self.capture_thread = None

        # hardware trigger only:
        if self.trigger_type == "hardware":
            self.frame_queue = Queue()
            self.stop_event = Event()

        

    def _set_exposure(self, camera, exposure=None):
        pass

    def read_from_buffer(self, buffer:list,position:int=0):
        '''reads from the camera buffer
        Args:
            position: the buffer position to read from
        '''
        if position > len(buffer):
            raise ValueError(f"Error : Buffer value of {position} is larger than buffer size of {len(self.buffer)}")
        safe_read = position 
        image = buffer[safe_read]
        return image

    def create_capture_thread(self)-> Thread:
        if self.trigger_type == "software" and self.capture_type == "continuous":
            if len(self.buffer) < 1:
                raise ValueError(
                    "software + continuous needs camera_settings.buffer_size >= 1 "
                    f"(got {self.camera_settings.get('buffer_size')!r})"
                )
            software_thread = Thread(
                target=self.__software_continuous_read,
                daemon=True
            )
            return software_thread
        if self.trigger_type == "hardware":
            if len(self.buffer) < 1:
                raise ValueError(
                    "hardware trigger needs camera_settings.buffer_size >= 1 "
                    f"(got {self.camera_settings.get('buffer_size')!r})"
                )
            if not hasattr(self, "wait_for_frame"):
                raise NotImplementedError(
                    f"{type(self).__name__} has no wait_for_frame, so no hardware trigger"
                )
            hardware_thread = Thread(
                target=self.wait_for_frame,
                args=(self.frame_queue, self.stop_event),
                daemon=True,
            )
            return hardware_thread
        return None

    def __software_continuous_read(self):
        '''captures images into the buffer for as long as the service runs (daemon thread: it ends with the process).'''
        while True:
            try:
                image = self.capture_image()
            except Exception:
                # one failed grab 
                logging.exception("Continuous capture failed, retrying")
                sleep(self.frame_pace)
                continue
            position = (self.buffer_position + 1) % len(self.buffer)
            self.buffer[position] = image
            self.buffer_position = position
            sleep(self.frame_pace)

    def move_frame_to_buffer(self, timeout_s: float = 5.0):
        '''hardware trigger: moves the oldest frame from frame_queue into the buffer.
        Raises:
            TimeoutError: no frame arrived within timeout_s.
            CameraLossError: wait_for_frame reported the camera lost.'''
        try:
            frame = self.frame_queue.get(timeout=timeout_s)
        except Empty:
            raise TimeoutError(f"No hardware-triggered frame within {timeout_s} s") from None
        if isinstance(frame, Exception):
            raise frame
        position = (self.buffer_position + 1) % len(self.buffer)
        self.buffer[position] = frame
        self.buffer_position = position

    def connect_to_camera(self):
        """Connect to the camera based on the specified camera type.
        Raises:
            Exception: If the camera type is unsupported or if connection fails."""
        # Open the default OpenCV camera and store on the instance so
        # subsequent calls to `capture_image` can use the same handle.
        self.camera = cv2.VideoCapture(0)
        if not getattr(self.camera, "isOpened", lambda: False)():
            try:
                self.camera.release()
            except Exception:
                pass
            self.camera = None
            self.cam = None
            raise Exception("Failed to open OpenCV camera.")
        # Alias used by main shutdown: disconnect_camera(camera.cam)
        self.cam = self.camera
        logging.info(f"connected to camera {self.camera.getBackendName()}")
        return self.camera

    def capture_image(self, timeout_ms=0):
        """Capture an image from the camera and return it as a numpy array.
        Returns:
            numpy.ndarray: The captured image.
        Raises:
            Exception: If the camera type is unsupported or if image capture fails."""

        ret, frame = self.camera.read()
        if not ret:
            raise Exception("Failed to capture image from OpenCV camera.")
        logging.info(f"Image captured")
        return frame

    def disconnect_camera(self, camera=None) -> None:
        """Release the OpenCV capture. Subclasses typically override this."""
        handle = camera if camera is not None else self.camera
        if handle is None:
            return
        try:
            handle.release()
        except Exception:
            logging.debug("Failed releasing OpenCV camera", exc_info=True)
        if self.camera is handle:
            self.camera = None
        if self.cam is handle:
            self.cam = None
        logging.info("OpenCV camera disconnected")

class CameraHeightMap(Camera):
    "Used for height map cameras"
    "As of now not useful, but be aware that you are using height map images with this class."

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    def connect_to_camera(self, timeout=30):
        pass

    def capture_image(self) -> ndarray:
        pass

    def disconnect_camera(self, camera=None) -> None:
        pass
