from dependencies.state_machine.base_functions.state import State

class GetBestFrameState(State):
    ''''''
    def __init__(self, state_machine, frames:list, state_id = "Get Best Frame"):
        super().__init__(state_machine, state_id)

    def tick(self):
        ''''''

