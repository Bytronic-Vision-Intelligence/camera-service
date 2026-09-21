from dependencies.CameraLibrary.cameras import Camera
import cv2
import logging
from pathlib import Path
from random import randint
from queue import Empty, Full, Queue
from threading import Event
import os


class DummyCamera(Camera):
    """A camera class used for testing with images from a file, not using any real camera.

    Reads ``dummy_location`` / ``file_type`` from ``self.camera_config`` after
    ``main.set_camera_class`` injects the service camera section.
    """

    def __init__(self):
        super().__init__()
        self.frame_list = []

    def connect_to_camera(self):
        directory = self.camera_config.get("dummy_location")
        if not directory:
            raise RuntimeError("camera.dummy_location is required for dummy cameras")
        extension = str(self.camera_config.get("file_type") or ".png")
        if not extension.startswith("."):
            extension = f".{extension}"
        self._get_frame_list(Path(directory), extension)

    def _get_frame_list(self, directory_path: Path, extension: str = ".png"):
        """Load image paths from a directory."""
        self.frame_list = []
        for root, _dirs, files in os.walk(directory_path):
            for file in files:
                if file.endswith(extension):
                    self.frame_list.append(os.path.join(root, file))
        if not self.frame_list:
            raise RuntimeError(f"No dummy frames found in {directory_path}")

    def capture_image(self, timeout_ms=0):
        """Return a random frame from the current frame list (keeps 16-bit depth)."""
        if not self.frame_list:
            raise RuntimeError("No dummy frames found in dummy_location")
        return cv2.imread(
            self.frame_list[randint(0, len(self.frame_list) - 1)],
            cv2.IMREAD_UNCHANGED,
        )

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
            fps = float(self.trigger_config.get("continuous_fps", 2))
        except (TypeError, ValueError):
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
        pass
