from .base_functions.state import State
from mqtt_client import MQTTConnectionError
from dependencies.mqtt.mqtt_functions import packetize_results
from logging import info
from time import sleep
from sys import getsizeof
class PublishState(State):
    ''''''
    def __init__(self, state_machine,data:dict, state_id = "Publish State"):
        super().__init__(state_machine, state_id)
        self.data = data

    def enter(self):
        super().enter()
        if not self.my_state_machine.mqtt_client.is_connected:
            self.my_state_machine.mqtt_client.connect()
        return

    def tick(self):
        ''''''
        
        self.my_state_machine.mqtt_client.publish(
            self.data
        )

        self.__return_idle()

    def __return_idle(self):
        from .idle_state import IdleState
        self.my_state_machine.change_state(
            IdleState(self.my_state_machine)
        )