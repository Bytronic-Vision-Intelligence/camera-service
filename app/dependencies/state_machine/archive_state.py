from .base_functions.state import State
from ..loadConfig import require
from ..archive_functions import archive_image, build_archive_filename, filename_categories
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
        archive_params = require(self.archive_config, "archive_parameters")
        archive_filename = build_archive_filename(
            filename_categories(archive_params),
            values={
                "camera_id": require(self.camera_config, "camera_id"),
                "camera_type": require(self.camera_config, "camera_type"),
                "image_type": self.image.get("image_id"),
                "sku_id": self.image.get("sku_id"),
                "datetime": self.timestamp,
                "verdict": self.image.get("verdict"),
            },
        )

        archive_image(
            self.image.get("image"),
            require(self.archive_config, "archive_directory"),
            archive_filename,
            archive_params,
            require(self.camera_config, "camera_id"),
        )
        self._return_idle()

    def exit(self):
        self.image = []
        super().exit()
