from .base_functions.state_machine import StateMachine
from ..CameraLibrary.cameras import Camera
from ..loadConfig import require
from ..image_functions import resolve_image_outputs

class CameraSM(StateMachine):
    def __init__(self, config:dict):
        camera_config = require(config,"service")
        self.camera = self.__set_camera_class(
            camera_config
        )
        self.__set_camera_details(
            camera_config
        )
        self.camera.connect_to_camera()

        self.image_outputs = resolve_image_outputs(config)
        self.burst_length = require(camera_config.get("camera_settings"),"burst_length")
        self.exposure = require(camera_config.get("camera_settings"), "exposure")
        self.state = self.set_idle()
        self.archiving = camera_config.get("archiving")

        super().__init__(config)


    def __set_camera_class(self,camera_config: dict):
        """Construct a camera backend, inject service config, and connect.

        All backends take no constructor arguments; domain knobs are set on the
        instance before ``connect_to_camera`` so they never call back into
        ``loadConfig``.
        """
        trigger_config = require(camera_config,"trigger")
        camera_settings = require(camera_config,"camera_settings")
        lights_config = require(camera_config,"lights")
        camera_type = require(camera_config.get("camera"), "camera_type")

        if not camera_type:
            raise ValueError("Camera type cannot be empty.")

        if camera_type == "opencv":
            camera = Camera()
        elif camera_type == "dummy":
            from dependencies.CameraLibrary.cameras_dummy import DummyCamera
            camera = DummyCamera()
        elif camera_type == "pylon":
            from dependencies.CameraLibrary.cameras_pylon import PylonCamera
            camera = PylonCamera()
        elif camera_type == "gige":
            from dependencies.CameraLibrary.cameras_gige import GigeCamera
            camera = GigeCamera()
        elif camera_type == "flir":
            from dependencies.CameraLibrary.cameras_flir import FlirCamera
            camera = FlirCamera()
        elif camera_type == "ljs":
            from dependencies.CameraLibrary.cameras_ljs import LJSCamera
            camera = LJSCamera()
        else:
            raise ValueError(f"Unsupported camera type: {camera_type}")

        camera.camera_config = camera_config
        camera.trigger_config = trigger_config
        camera.camera_settings = camera_settings or {}
        camera.buffer = [0 for _ in range(camera_settings.get("buffer_size"))]
        camera.lights_config = lights_config if isinstance(lights_config, dict) else {}
        return camera

    def __set_camera_details(self, camera_config:dict):
        '''sets the trigger_type and capture_type variables of the
        camera object'''
        trigger_config = require(camera_config, "trigger")
        trigger_type = str(require(trigger_config, "trigger_type")).strip().lower()
        self.camera.trigger_type = trigger_type

        capture_config = require(camera_config, "trigger")
        capture_type = str(require(capture_config, "capture_type")).strip().lower()
        self.camera.capture_type = capture_type