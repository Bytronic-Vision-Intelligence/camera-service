from .base_functions.state import State
from .capture_state import CaptureState
from dependencies.mqtt.topic import check_for_messages

class IdleState(State):
    '''A state to control the idle process'''
    def __init__(self, state_machine, state_id = "Idle State"):
        super().__init__(state_machine, state_id)
        self.camera = self.my_state_machine.camera

    def tick(self):
        '''Waits for a message to be received from the target, 
        then switches to the appropriate state'''
        if not self.__mqtt_watchdog():return
        message = check_for_messages(self.my_state_machine.trigger_request)
        if not "matches_found" in message: return
        if message.get("matches_found") == 0: return

        if self.camera.trigger_type == "software":
            self.my_state_machine.change_state(
                CaptureState(self.my_state_machine)
            )
        if self.camera.trigger_type == "hardware":
            ''''''

    def __mqtt_watchdog(self):
        '''a simple process that checks the state of the client and 
        reconnects if the beoker disconnects due to being idle'''
        if self.my_state_machine.mqtt_client.is_connected: return True
        print("Info : mqtt client disconnected, reconnecting")
        self.my_state_machine.mqtt_client.connect()
        return False