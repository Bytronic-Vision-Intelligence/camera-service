from dependencies.state_machine.base_functions.state import State
from ..CameraLibrary.cameras import Camera
from .publish_state import PublishState
from .get_best_frame_state import GetBestFrameState
from time import sleep
from datetime import datetime
from dependencies.cycle_timing import mark, mark_frame_done


def trigger_option(camera, name: str, default: bool) -> bool:
    """A capture switch from the camera's ``trigger`` config section.

    ``pace_last_frame`` (default true): pause 2 x frame_pace after the last frame of a
        capture as well as between burst frames, as camera-service always has. False
        publishes the last frame straight away.
    ``early_exposure`` (default false): write the first frame's exposure while the
        trigger delay runs, and skip writes when the camera already has that exposure.
        False writes it after the delay on every frame, as camera-service always has.
    """
    return bool((getattr(camera, "trigger_config", None) or {}).get(name, default))


def apply_exposure(camera, exposure, frame_index):
    """Write the exposure for burst frame ``frame_index`` (ms, or a list of ms per
    frame whose last value repeats). With ``early_exposure`` on, a write is skipped when
    the camera already has that exposure. Returns True when it wrote one."""
    if exposure is None:
        return False
    if type(exposure) == list:
        exposure = exposure[min(frame_index, len(exposure) - 1)]
    value = exposure * 1000
    skip_unchanged = trigger_option(camera, "early_exposure", False)
    if skip_unchanged and getattr(camera, "_applied_exposure", None) == value:
        return False
    camera._set_exposure(camera.cam, value)
    camera._applied_exposure = value
    return True


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
        if apply_exposure(self.camera, self.exposure, len(self.images_captured)):
            mark(self.my_state_machine, "set exposure")

        image = self.__get_image(self.camera)
        self.images_captured.append(image)

        last_frame = self.burst_length == self.my_state_machine.burst_length
        if not last_frame or trigger_option(self.camera, "pace_last_frame", True):
            sleep(self.camera.frame_pace*2)
            mark(self.my_state_machine, "pace sleep")
        if last_frame:
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
        self.images_captured = []
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
        timing = getattr(self.my_state_machine, "timing", None)
        camera.on_step = lambda name: mark(self.my_state_machine, name)
        camera.on_frame_done = lambda: mark_frame_done(self.my_state_machine)
        try:
            image = self.__read_image(camera)
        except Exception:
            camera._applied_exposure = None     # a failed capture may mean a reset camera
            raise
        finally:
            camera.on_step = camera.on_frame_done = None
        if timing is not None and timing.frame_done is None:
            mark_frame_done(self.my_state_machine)   # cameras without their own timing points
        capture_time = datetime.now()
        return {
            "image": image,
            "capture_time": capture_time,
            "metadata": getattr(camera, "last_metadata", None) or None    # per-frame extras, e.g. Gocator geometry + scan start
        }

    def __read_image(self, camera:Camera):
        if camera.capture_thread==None: # only software + single
            image =  camera.capture_image()
        else:
            if camera.trigger_type == "hardware":
                camera.move_frame_to_buffer()
            image =  camera.read_from_buffer(
                camera.buffer,
                camera.buffer_position
            )
        return image
