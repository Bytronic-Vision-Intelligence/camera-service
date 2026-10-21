from dependencies.state_machine.camera_state_machine import CameraSM
from dependencies import loadConfig


def main(argv=None) -> int:
    args = loadConfig.parse_cli(argv)
    config = loadConfig.get_config(args.config)
    camera_sm = CameraSM(config)
    while True:
        camera_sm.runtime()

if __name__ == "__main__":
    raise SystemExit(main())
