import cv2
import logging
from numpy import ndarray
from threading import Event, Thread
from time import sleep

class Camera:
    def __init__(self):
        self.camera = any
        self.cam = self.camera
        self.framerate = 60
        self.frame_pace = 1/self.framerate

        self.trigger_type = str()
        self.capture_type = str()

        self.buffer = []
        self.buffer_position = 0

        self.capture_thread = self.__create_capture_thread()
        if self.capture_thread!=None:self.capture_thread.start()

        # Injected by main.set_camera_class before connect_to_camera().
        # are these really nessacary? can we not set this up in a better way?
        self.camera_config: dict = {}
        self.trigger_config: dict = {}
        self.camera_settings: dict = {}
        self.lights_config: dict = {}

    def read_from_buffer(self, buffer:list,position:int=0):
        '''reads from the camera buffer
        Args:
            position: the buffer position to read from
        '''
        if position > len(buffer):
            raise ValueError(f"Error : Buffer value of {position} is larger than buffer size of {len(self.buffer)}")
        safe_read = position-1%len(self.buffer)
        image = buffer[safe_read]
        return image

    def __create_capture_thread(self)-> Thread:
        if self.capture_type == "software":
            software_thread = Thread(
                target=self.__software_continuous_read(),
                daemon=True
            )
            return software_thread
        if self.capture_type == "hardware":
            hardware_thread = Thread(
                target=self.camera.wait_for_frame,
                args=(self.capture_thread, Event),
                daemon=True,
            )
            return hardware_thread
        return None

    def __software_continuous_read(self):
        '''captures an image, adds it to the buffer continuously'''
        image = self.capture_image()
        position = position+1 % len(self.buffer)
        self.buffer[position] = image
        sleep(self.frame_pace)

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

    def __init__(self):
        super().__init__()

    def connect_to_camera(self, timeout=30):
        pass

    def capture_image(self) -> ndarray:
        pass

    def disconnect_camera(self, camera=None) -> None:
        pass
