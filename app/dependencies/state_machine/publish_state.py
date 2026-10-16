from .base_functions.state import State
from .archive_state import ArchiveState
from ..image_functions import apply_image_format,encode_image_to_bytes,image_encoding
import base64
from ..CameraLibrary.cameras import Camera
class PublishState(State):
    ''''''
    def __init__(self, state_machine,image, state_id = "Publish State"):
        super().__init__(state_machine, state_id)
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
        image_encoded = encode_image_to_bytes(self.image.get("image"))
        for output in self.my_state_machine.image_outputs:
            variant = apply_image_format(self.image.get("image"), output)
            packet = {
                "image": base64.b64encode(image_encoded).decode("ascii"),
                "date_time": str(self.image.get("capture_time")),
                "image_id": output["id"],
                "encoding": image_encoding(variant),
            }
        
            self.my_state_machine.mqtt_client.publish(
                self.my_state_machine.service_output.topic,
                packet
            )

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