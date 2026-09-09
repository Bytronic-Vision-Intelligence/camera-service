from dependencies.CameraLibrary.cameras import Camera
import cv2
import logging
from numpy import ndarray
from pathlib import Path
from queue import Empty, Full, Queue
from random import randint
from threading import Event
import os
import time

class DummyCamera(Camera):
    '''A camera class used for testing with images from a file, not using any real camera'''

    def __init__(self, directory_path:Path, extension:str=".png"):
        self.camera = None
        self.cam = None

        self._get_frame_list(directory_path, extension)

    def connect_to_camera(self):        
        """ Connect to the camera based on the specified camera type.
        Raises:
            Exception: If the camera type is unsupported or if connection fails."""
        pass

    def _get_frame_list(self,directory_path:Path, extension:str=".png"):
        '''returns a list of images from a direcory
        Args:
            directory_path: the path to the directory
            extension: a string containing the image extension
        '''
        self.frame_list = []
        frame_path_list = []
        for root, dirs, files in os.walk(directory_path):
            for file in files:
                if file.endswith(extension):
                    self.frame_list.append(os.path.join(root, file))
    
    def capture_image(self, timeout_ms):
        """Returns a random frame from the current frame list.
        Returns:
            numpy.ndarray: The captured image.
        Raises:
            Exception: If the camera type is unsupported or if image capture fails."""
        if not self.frame_list:
            raise RuntimeError("No dummy frames found in dummy_location")
        
        return cv2.imread(self.frame_list[randint(0, len(self.frame_list)-1)])

    def wait_for_frame(
        self,
        queue: Queue,
        stop_event: Event,
        camera=None,
        timeout_ms: int = 5000,
        is_converted: bool = False,
    ):
        """Simulate free-running acquisition by enqueueing frames at continuous_fps."""
        try:
            from dependencies import loadConfig

            trigger_cfg = loadConfig.get_section("trigger")
            fps = float(trigger_cfg.get("continuous_fps", 2))
        except Exception:
            fps = 2.0
        interval = 1.0 / max(fps, 0.1)

        logging.info("Dummy continuous acquisition at %.2f fps", fps)
        while not stop_event.is_set():
            try:
                frame = self.capture_image(timeout_ms=timeout_ms)
            except Exception as exc:
                logging.error("Dummy continuous capture failed: %s", exc, exc_info=True)
                if stop_event.wait(interval):
                    break
                continue

            if frame is None:
                if stop_event.wait(interval):
                    break
                continue

            try:
                queue.put(frame, timeout=0.5)
            except Full:
                try:
                    queue.get_nowait()
                except Empty:
                    pass
                try:
                    queue.put(frame, timeout=0.5)
                except Full:
                    pass

            if stop_event.wait(interval):
                break

    def disconnect_camera(self, camera=None) -> None:
        """Release the OpenCV capture. Subclasses typically override this."""
        pass
