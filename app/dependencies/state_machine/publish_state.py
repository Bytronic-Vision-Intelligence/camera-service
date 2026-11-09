from .base_functions.state import State
from .archive_state import ArchiveState
from ..image_functions import (
    apply_image_format,
    build_image_topic,
    encode_image_to_bytes,
    encode_png_tiles,
    image_encoding,
    pack_binary_packet,
)
import base64
from ..CameraLibrary.cameras import Camera
from numpy import uint16, uint8, float32 ##, unique
from sys import getsizeof
class PublishState(State):
    ''''''
    def __init__(self, state_machine,image, state_id = "Publish State"):
        super().__init__(state_machine, state_id)
        if type(image) == list:
            self.image:dict = image[0]
        else:
            self.image:dict = image
        self.data = {}

    def enter(self):
        super().enter()
        camera:Camera = self.my_state_machine.camera
        self.trigger_config = camera.trigger_config
        self.camera_config = camera.camera_config
        self.camera_settings = camera.camera_settings
        self.planck_constants = None
        if self.camera_config.get("camera_type").lower() == "flir":
            self.planck_constants = camera.planck_constants
        elif self.camera_settings.get("planck_constants"):
            self.planck_constants = self.camera_settings.get("planck_constants")
        self.is_archived = self.my_state_machine.archiving.get("is_archived")
        if not self.my_state_machine.mqtt_client.is_connected:
            self.my_state_machine.mqtt_client.connect()
        return

    def tick(self):
        ''''''
        ## image_details = unique(self.image.get("image"))
        camera_details = {**self.camera_config, **self.camera_settings}
        metadata = dict(self.image.get("metadata") or {})
        if self.planck_constants:
            camera_details["planck_constants"] = self.planck_constants
            metadata["planck_constants"] = self.planck_constants
        # print(f"image has the following values {unique(image_details)}")
        for output in self.my_state_machine.image_outputs:
            variant = apply_image_format(self.image.get("image"), output)
            image_type = self.__get_type(output.get("image_type"))
            tiles = int(output.get("tiles") or 1)
            if image_type is uint16 and tiles > 1:
                # PNG in strips encoded side by side: the list of strips, top to bottom
                image_bytes = encode_png_tiles(variant, tiles)
            else:
                image_bytes = encode_image_to_bytes(variant, image_type)

            packet = {
                "date_time": str(self.image.get("capture_time")),
                "image_id": output["id"],
                "encoding": image_encoding(variant),
                "camera_details": camera_details
            }
            if metadata:   # sth in metadata e.g. Gocator geometry + scan start, thermal Planck constants
                packet["metadata"] = metadata
            if output.get("payload_format") == "binary":
                # image bytes as they are after a JSON header, no base64 (pack_binary_packet)
                payload = pack_binary_packet(packet, image_bytes)
                packet_size = len(payload)/(1024 * 1024)
            else:
                if isinstance(image_bytes, list):
                    image_field = [base64.b64encode(part).decode("ascii") for part in image_bytes]
                else:
                    image_field = base64.b64encode(image_bytes).decode("ascii")
                payload = {"image": image_field, **packet}
                packet_size = sum(getsizeof(part) for part in (image_field if isinstance(image_field, list)
                                                               else [image_field]))/(1024 * 1024)
            print(f"Info : packet is {packet_size} MB")

            topic = build_image_topic(
                self.my_state_machine.service_output.topic,
                output.get("topic_end"),
            )
            self.my_state_machine.mqtt_client.publish(topic, payload)

        if self.is_archived:
            self.my_state_machine.change_state(
                ArchiveState(
                    {
                        "image":self.image.get("image"),
                        "capture_time":self.image.get("capture_time"),
                        "image_id":packet.get("image_id")
                    },
                    self.my_state_machine,
                )
            )
            return

        self._return_idle()

    def exit(self):
        self.image = []
        super().exit()

    def __get_type(self, type_string: str):
        if "uint8" in type_string:
            return uint8
        if "uint16" in type_string:
            return uint16
        if "float32" in type_string:
            return float32
        raise ValueError(f"Unsupported image_type: {type_string!r}")