from .base_functions.state_machine import StateMachine
from ..CameraLibrary.cameras import Camera
from ..loadConfig import require
from ..image_functions import resolve_image_outputs


def set_camera_class(camera_config: dict):
    """Construct a camera backend with the service config."""
    trigger_config = require(camera_config, "trigger")
    require(trigger_config, "trigger_type")
    require(trigger_config, "capture_type")
    require(camera_config, "camera_settings")
    require(camera_config, "lights")
    camera_type = require(camera_config.get("camera"), "camera_type")

    if not camera_type:
        raise ValueError("Camera type cannot be empty.")

    if camera_type == "opencv":
        camera = Camera(camera_config)
    elif camera_type == "dummy":
        from dependencies.CameraLibrary.cameras_dummy import DummyCamera
        camera = DummyCamera(camera_config)
    elif camera_type == "pylon":
        from dependencies.CameraLibrary.cameras_pylon import PylonCamera
        camera = PylonCamera(camera_config)
    elif camera_type == "gige":
        from dependencies.CameraLibrary.cameras_gige import GigeCamera
        camera = GigeCamera(camera_config)
    elif camera_type == "flir":
        from dependencies.CameraLibrary.cameras_flir import FlirCamera
        camera = FlirCamera(camera_config)
    elif camera_type == "ljs":
        from dependencies.CameraLibrary.cameras_ljs import LJSCamera
        camera = LJSCamera(camera_config)
    elif camera_type == "gocator":
        from dependencies.CameraLibrary.cameras_gocator import GocatorCamera
        camera = GocatorCamera(camera_config)
    else:
        raise ValueError(f"Unsupported camera type: {camera_type}")

    return camera


class CameraSM(StateMachine):
    def __init__(self, config:dict):
        camera_config = require(config,"service")
        self.camera = set_camera_class(camera_config)
        
        self.camera.connect_to_camera()
        self.camera.capture_thread = self.camera.create_capture_thread()
        if self.camera.capture_thread!=None:self.camera.capture_thread.start()

        self.image_outputs = resolve_image_outputs(camera_config)
        self.burst_length = require(camera_config.get("camera_settings"),"burst_length")
        self.state = self.set_idle()
        self.archiving = camera_config.get("archiving")

        super().__init__(config)
        self.service_id = camera_config["camera"].get("camera_id")