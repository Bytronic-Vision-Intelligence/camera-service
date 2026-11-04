from .base_functions.state import State
from .capture_state import CaptureState
from dependencies.mqtt.topic import check_for_messages
from time import sleep
from dependencies.cycle_timing import start, mark
from .capture_state import apply_exposure
from time import monotonic

class IdleState(State):
    '''A state to control the idle process'''
    def __init__(self, state_machine, state_id = "Idle State"):
        super().__init__(state_machine, state_id)
        self.camera = self.my_state_machine.camera
        self.ready_for_trigger = False
        self.last_ready_check = 0.0

    def tick(self):
        '''Waits for a message to be received from the target, 
        then switches to the appropriate state'''
        if not self.__mqtt_watchdog():return
        message = check_for_messages(self.my_state_machine.trigger_request)
        if not self.my_state_machine.service_id in message:
            self.__prepare_for_trigger()
            return
        if message.get(self.my_state_machine.service_id) == 0: return

        self.my_state_machine.message_id = message.get("message_id")
        start(self.my_state_machine, self.my_state_machine.service_id, message.get("message_id"))
        if self.camera.trigger_type == "software":
            delay_end = monotonic() + self.__get_delay(message.get(self.my_state_machine.service_id))
            # write the first frame's exposure while waiting, not after the delay
            if apply_exposure(self.camera, message.get("exposure"), 0):
                mark(self.my_state_machine, "set exposure")
            sleep(max(delay_end - monotonic(), 0.0))
            mark(self.my_state_machine, "delay")
            self.my_state_machine.change_state(
                CaptureState(
                    self.my_state_machine,
                    images_captured=[],
                    exposure=message.get("exposure")
                )
            )
        if self.camera.trigger_type == "hardware":
            ''''''

    def __prepare_for_trigger(self):
        """While idle, let the camera get ready for the next trigger (checked at most
        every 0.1 s, and no more once it reports ready)."""
        if self.ready_for_trigger or monotonic() - self.last_ready_check < 0.1:
            return
        self.last_ready_check = monotonic()
        prepare = getattr(self.camera, "prepare_for_trigger", None)
        self.ready_for_trigger = True if prepare is None else bool(prepare())

    @staticmethod
    def __get_delay(instruction) -> float:
        '''returns the delay in s before capture, taken from an instruction of the form
        [trigger, delay], waited once here so a burst of captures is not delayed per frame
        Args:
            instruction: the value sent under this camera's service_id
        Returns:
            the delay in s, 0 when none is given or it is not a valid number
        '''
        if not isinstance(instruction, list) or len(instruction) < 2: return 0.0
        try:
            return max(float(instruction[1]), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def __mqtt_watchdog(self):
        '''a simple process that checks the state of the client and 
        reconnects if the beoker disconnects due to being idle'''
        if self.my_state_machine.mqtt_client.is_connected: return True
        print("Info : mqtt client disconnected, reconnecting")
        self.my_state_machine.mqtt_client.connect()
        return False