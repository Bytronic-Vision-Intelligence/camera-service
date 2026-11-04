"""One log line per capture cycle: when the trigger arrived, how long each step took,
and the clock time the camera finished (for comparing with dio's light and laser times)."""

from datetime import datetime, timedelta
from time import perf_counter


class CycleTiming():
    def __init__(self, camera_id, message_id):
        self.camera_id = camera_id
        self.message_id = message_id
        self.received = datetime.now()
        self.start = self.last = perf_counter()
        self.steps = []
        self.frame_done = None

    def mark(self, step):
        now = perf_counter()
        self.steps.append((step, now - self.last))
        self.last = now

    def mark_frame_done(self):
        '''the camera has delivered the frame: the scan or exposure is over'''
        self.mark("capture")
        self.frame_done = self.received + timedelta(seconds=self.last - self.start)

    def line(self, outcome):
        steps = ", ".join(f"{name} {seconds * 1000:.0f}" for name, seconds in self.steps)
        done = f"; frame done {self.frame_done:%H:%M:%S.%f}"[:-3] if self.frame_done else ""
        return (f"Timing : {self.camera_id} {self.message_id} received {self.received:%H:%M:%S.%f}"[:-3]
                + f"{done}; {steps}; total {(self.last - self.start) * 1000:.0f} ms ({outcome})")


def start(state_machine, camera_id, message_id):
    state_machine.timing = CycleTiming(camera_id, message_id)


def mark(state_machine, step):
    timing = getattr(state_machine, "timing", None)
    if timing is not None:
        timing.mark(step)


def mark_frame_done(state_machine):
    timing = getattr(state_machine, "timing", None)
    if timing is not None:
        timing.mark_frame_done()


def report(state_machine, outcome):
    timing = getattr(state_machine, "timing", None)
    if timing is None:
        return
    print(timing.line(outcome), flush=True)
    state_machine.timing = None
