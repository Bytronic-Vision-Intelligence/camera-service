from .cameras import *
from dependencies.image_functions import encode_image_to_bytes
from base64 import b64encode

class CameraHandler():

    def set_camera_class(camera_type: str):
        if not camera_type:
            raise ValueError("Camera type cannot be empty.")
        
        if camera_type == "opencv":
            camera = Camera()
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
        
        camera.connect_to_camera()
        return camera

    def package_image(image, date_time):
        image_bytes = encode_image_to_bytes(image)
        packet = {}
        packet["image"] = b64encode(image_bytes).decode("ascii")
        packet["date_time"] = date_time.decode("utf-8")
        return packet