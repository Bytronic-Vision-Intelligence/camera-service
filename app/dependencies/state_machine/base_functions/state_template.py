from app.dependencies.state_machine.base_functions.state import State

class TemplateState(State):
    ''''''
    def __init__(self, state_machine, state_id = ""):
        super().__init__(state_machine, state_id)

    def enter(self):
        ''''''

    def tick(self):
        ''''''

    def exit(self):
        ''''''
