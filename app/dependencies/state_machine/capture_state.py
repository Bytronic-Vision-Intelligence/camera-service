from dependencies.state_machine.base_functions.state import State
from ..CameraLibrary.cameras import Camera
from .publish_state import PublishState
from .get_best_frame_state import GetBestFrameState
from time import sleep
from datetime import datetime
from dependencies.cycle_timing import mark, mark_frame_done
class CaptureState(State):
    ''''''
    def __init__(self, 
            state_machine,
            images_captured:list=[],
            burst_length:int = 1, 
            exposure:any = None,
            state_id = "Capture State"
        ):
        super().__init__(state_machine, state_id)
        self.images_captured = images_captured
        self.burst_length = burst_length
        self.exposure = exposure
    
    def enter(self):
        self.camera:Camera = self.my_state_machine.camera
        return super().enter()

    def tick(self):
        ''''''
        if self.exposure != None:
            self.__set_exposure(self.exposure)
            mark(self.my_state_machine, "set exposure")

        image = self.__get_image(self.camera)
        self.images_captured.append(image)

        if self.burst_length == self.my_state_machine.burst_length:
            self.__next_state()
            return
        
        self.my_state_machine.change_state(
            CaptureState(
                self.my_state_machine,
                self.images_captured,
                self.burst_length+1,
                exposure=self.exposure
            )
        )

    def exit(self):
        sleep(self.camera.frame_pace*2)
        mark(self.my_state_machine, "pace sleep")
        self.images_captured = []
        return super().exit()

    def __set_exposure(self, exposure):
        exposure = self.exposure
        if type(exposure) == list:
            # one exposure per burst frame; a list shorter than the burst repeats its last value
            exposure = exposure[min(len(self.images_captured), len(exposure) - 1)]
        self.camera._set_exposure(self.camera.cam, exposure*1000)

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
        timing = getattr(self.my_state_machine, "timing", None)
        camera.on_step = lambda name: mark(self.my_state_machine, name)
        camera.on_frame_done = lambda: mark_frame_done(self.my_state_machine)
        try:
            image = self.__read_image(camera)
        finally:
            camera.on_step = camera.on_frame_done = None
        if timing is not None and timing.frame_done is None:
            mark_frame_done(self.my_state_machine)   # cameras without their own timing points
        capture_time = datetime.now()
        return {
            "image": image,
            "capture_time": capture_time
        }

    def __read_image(self, camera:Camera):
        if camera.capture_thread==None:
            image =  camera.capture_image()
        else:
            image =  camera.read_from_buffer(
                camera.buffer,
                camera.buffer_position
            )
        return image