from dependencies.state_machine.base_functions.state import State
from ..CameraLibrary.cameras import Camera
from publish_state import PublishState
from get_best_frame_state import GetBestFrameState
from time import sleep
class CaptureState(State):
    ''''''
    def __init__(self, 
            state_machine,
            images_captured:list=[],
            burst_length:int = 1, 
            state_id = "Capture State"
        ):
        super().__init__(state_machine, state_id)
        self.images_captured = images_captured
        self.burst_length = burst_length
    
    def enter(self):
        self.camera:Camera = self.my_state_machine.camera
        return super().enter()

    def tick(self):
        ''''''
        image = self.__get_image(self.camera)
        self.images_captured.append(image)

        if self.burst_length == self.my_state_machine.burst_length:
            self.__next_state()
            return
        
        self.my_state_machine.change_state(
            CaptureState(
                self.my_state_machine,
                self.images_captured,
                self.burst_length+1
            )
        )

    def exit(self):
        sleep(self.camera.frame_pace)
        return super().exit()

    def __next_state(self):
        if self.my_state_machine.burst_length ==1:
            self.my_state_machine.change_state(
                PublishState(
                    self.my_state_machine,
                    self.images_captured
                )
            )
            return
        self.my_state_machine.change_state(
            GetBestFrameState(
                self.my_state_machine,
                self.images_captured
            )
        )

    def __get_image(self,camera:Camera):
        if camera.capture_thread==None:
            return camera.capture_image()
        
        return camera.read_from_buffer(
            camera.buffer,
            camera.buffer_position
        )