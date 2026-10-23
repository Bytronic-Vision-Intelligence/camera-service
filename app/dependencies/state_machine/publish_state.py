from .base_functions.state import State
from .archive_state import ArchiveState
from ..image_functions import (
    apply_image_format,
    build_image_topic,
    encode_image_to_bytes,
    image_encoding,
)
import base64
from ..CameraLibrary.cameras import Camera
from numpy import unique, uint16, uint8, float32
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
        self.is_archived = self.my_state_machine.archiving.get("is_archived")
        if not self.my_state_machine.mqtt_client.is_connected:
            self.my_state_machine.mqtt_client.connect()
        return

    def tick(self):
        ''''''
        image_details = unique(self.image.get("image"))
        print(f"image has the following values {unique(image_details)}")
        for output in self.my_state_machine.image_outputs:
            variant = apply_image_format(self.image.get("image"), output)
            variant_encoded = encode_image_to_bytes(
                variant,
                self.__get_type(output.get("image_format")),
            )

            packet = {
                "image": base64.b64encode(variant_encoded).decode("ascii"),
                "date_time": str(self.image.get("capture_time")),
                "image_id": output["id"],
                "encoding": image_encoding(variant),
            }
            packet_size = getsizeof(packet.get("image"))/(1024 * 1024)
            print(f"Info : packet is {packet_size} MB")

            topic = build_image_topic(
                self.my_state_machine.service_output.topic,
                output.get("topic_end"),
            )
            self.my_state_machine.mqtt_client.publish(topic, packet)

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

        self.__return_idle()

    def exit(self):
        self.image = []
        super().exit()

    def __get_type(self, type_string: str | dict | None):
        if type_string is None or "uint8" in type_string or "colourmap" in type_string:
            return uint8
        if "uint16" in type_string or "Mono" in type_string or "mono" in type_string:
            return uint16
        if "float32" in type_string:
            return float32
