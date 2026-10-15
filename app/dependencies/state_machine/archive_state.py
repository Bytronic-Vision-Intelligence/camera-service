from .base_functions.state import State
from ..loadConfig import require
from ..archive_functions import archive_image
from ..CameraLibrary.cameras import Camera

class ArchiveState(State):
    ''''''
    def __init__(self,image:dict, state_machine, state_id = "Archive"):
        super().__init__(state_machine, state_id)
        self.image:dict = image
        self.camera:Camera = self.my_state_machine.camera

    def enter(self):
        ''''''
        self.camera_config = self.camera.camera_config
        self.timestamp = self.image.get("capture_time")
        self.archive_config = self.my_state_machine.archiving

    def tick(self):
        ''''''
        archive_filename = (
            f"cam_{require(self.camera_config.get('camera'), 'camera_id') or '0'}_"
            f"{require(self.camera_config.get('camera'), 'camera_type')}_"
            f"{self.image.get('image_id')}_{self.timestamp}"
        )

        archive_image(
            self.image.get("image"),
            require(self.archive_config, "archive_directory"),
            archive_filename,
            require(self.archive_config, "archive_parameters"),
            require(self.camera_config.get("camera"), "camera_id"),
        )
        self._return_idle()
