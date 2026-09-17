from queue import Queue

import numpy as np
import pytest

from fakes import FakeMQTTClient, FakeMQTTConfig, FakeThread

import main


def make_topics():
    return [
        {
            "name": "trigger",
            "topic": "project/camera/colour/trigger",
            "is_subscribe": True,
            "is_trigger": True,
        },
        {
            "name": "image",
            "topic": "project/camera/colour/image",
            "is_subscribe": False,
        },
    ]


def make_service(**overrides):
    service = {
        "camera": {
            "camera_type": "dummy",
            "camera_id": "colour",
            "capture_timeout": 10,
            "dummy_location": "../data/dummy",
            "file_type": "bmp",
        },
        "trigger": {
            "trigger_type": "software",
            "capture_type": "single",
        },
        "images": [
            {"default": {"image_format": None, "topic_end": None, "archive": False}}
        ],
        "archiving": {
            "is_archived": False,
            "archive_directory": "../data",
            "archive_parameters": {"archive_freq": "daily"},
        },
    }
    service.update(overrides)
    return service


def make_config(**service_overrides):
    return {
        "mqtt": {
            "mqtt_ip": "127.0.0.1",
            "mqtt_port": 1883,
            "topics": make_topics(),
        },
        "service": make_service(**service_overrides),
        "logging": {"level": "INFO"},
    }


class FakeCamera:
    def __init__(self):
        self.cam = object()
        self.captured = 0
        self.disconnected = False
        self.stopped = False
        self.camera_config = {}
        self.trigger_config = {}
        self.camera_settings = {}
        self.lights_config = {}

    def connect_to_camera(self):
        return None

    def capture_image(self, timeout_ms=None):
        self.captured += 1
        return np.zeros((4, 4), dtype=np.uint8)

    def stop_acquisition(self):
        self.stopped = True

    def disconnect_camera(self, cam):
        self.disconnected = True


def test_require_exits_naming_the_missing_key():
    with pytest.raises(SystemExit, match="mqtt"):
        main.require({}, "mqtt")


def test_topic_named_returns_image_base():
    assert main.topic_named(make_topics(), "image") == "project/camera/colour/image"


def test_topic_named_fills_placeholders():
    topics = [
        {"name": "image", "topic": "{project}/camera/{camera_id}/image"},
    ]
    assert (
        main.topic_named(topics, "image", {"project": "plant", "camera_id": "cam1"})
        == "plant/camera/cam1/image"
    )


def test_set_camera_class_injects_config(tmp_path):
    # Minimal bmp so DummyCamera.connect succeeds.
    frame = tmp_path / "frame.bmp"
    import cv2
    import numpy as np

    cv2.imwrite(str(frame), np.zeros((8, 8), dtype=np.uint8))

    camera = main.set_camera_class(
        "dummy",
        {
            "camera_type": "dummy",
            "dummy_location": str(tmp_path),
            "file_type": ".bmp",
        },
        {"trigger_type": "software", "capture_type": "single"},
        camera_settings={"pixel_format": "Mono14"},
        lights_config={"line": 0},
    )
    assert camera.camera_config["dummy_location"] == str(tmp_path)
    assert camera.trigger_config["capture_type"] == "single"
    assert camera.camera_settings["pixel_format"] == "Mono14"
    assert camera.lights_config["line"] == 0
    assert camera.capture_image(timeout_ms=10) is not None
    camera.disconnect_camera(None)


def test_publish_outputs_requires_archiving_keys():
    client = FakeMQTTClient(FakeMQTTConfig("127.0.0.1", 1883))
    outputs = [
        {
            "id": "default",
            "image_format": None,
            "rotate": None,
            "crop": None,
            "topic_end": None,
            "archive": False,
        }
    ]
    with pytest.raises(SystemExit, match="is_archived"):
        main.publish_outputs(
            client,
            np.zeros((4, 4), dtype=np.uint8),
            outputs,
            "project/camera/colour/image",
            {"camera_id": "colour", "camera_type": "dummy"},
            {},
        )


def test_main_answers_help_before_looking_for_a_config(monkeypatch):
    monkeypatch.setattr(
        main.loadConfig,
        "get_config",
        lambda supplied=None: pytest.fail("looked for a config before --help"),
    )
    with pytest.raises(SystemExit) as exit_info:
        main.main(["--help"])
    assert exit_info.value.code == 0


def test_the_config_shipped_in_the_repo_satisfies_what_main_requires():
    import yaml
    from pathlib import Path

    # Tracked shape is config.example.yaml — config.yaml is local/orchestrator only.
    config = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text()
    )
    mqtt = main.require(config, "mqtt")
    main.require(mqtt, "topics")
    service = main.require(config, "service")
    trigger = main.require(service, "trigger")
    main.require(service, "camera")
    main.require(service, "archiving")
    assert trigger["trigger_type"] in ("software", "hardware")
    assert trigger["capture_type"] in ("single", "continuous")


def test_main_software_single_captures_and_publishes(monkeypatch):
    config = make_config()
    monkeypatch.setattr(main.loadConfig, "get_config", lambda supplied=None: config)
    monkeypatch.setattr(main, "MQTTClient", FakeMQTTClient)
    monkeypatch.setattr(main, "MQTTConfig", FakeMQTTConfig)

    fake_camera = FakeCamera()
    monkeypatch.setattr(main, "set_camera_class", lambda *a, **k: fake_camera)

    def fake_start(ip, port, topic, queue, stop_event):
        # Matches run_software_single: {camera_id: ["trigger", delay]}
        queue.put('{"colour": ["trigger", 0.0]}')
        return FakeThread()

    monkeypatch.setattr(main, "start_subscribe_thread", fake_start)

    original = main.publish_outputs

    def wrap(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "publish_outputs", wrap)
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main.main(["--config", "stub.yaml"])
    assert fake_camera.captured == 1
    assert fake_camera.disconnected is True


def test_continuous_stream_command_on_off():
    assert main.continuous_stream_command('{"active": true}') is True
    assert main.continuous_stream_command('{"active": false}') is False
    assert main.continuous_stream_command("on") is True
    assert main.continuous_stream_command("off") is False
    assert main.continuous_stream_command('{"command": "start"}') is True
    assert main.continuous_stream_command('{"command": "stop"}') is False
    assert main.continuous_stream_command('{"command": "trigger"}') is None


def test_main_software_continuous_on_off(monkeypatch):
    config = make_config(
        trigger={
            "trigger_type": "software",
            "capture_type": "continuous",
            "trigger_delay": 0.01,
        }
    )
    monkeypatch.setattr(main.loadConfig, "get_config", lambda supplied=None: config)
    monkeypatch.setattr(main, "MQTTClient", FakeMQTTClient)
    monkeypatch.setattr(main, "MQTTConfig", FakeMQTTConfig)

    fake_camera = FakeCamera()
    monkeypatch.setattr(main, "set_camera_class", lambda *a, **k: fake_camera)

    def fake_start(ip, port, topic, queue, stop_event):
        # Size-1 queue: only the latest command is kept.
        queue.put('{"active": true}')
        return FakeThread()

    monkeypatch.setattr(main, "start_subscribe_thread", fake_start)

    original = main.publish_outputs

    def wrap(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "publish_outputs", wrap)
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main.main(["--config", "stub.yaml"])
    assert fake_camera.captured >= 1
    assert fake_camera.disconnected is True


def test_main_rejects_unknown_trigger_type(monkeypatch):
    config = make_config(trigger={"trigger_type": "internal", "capture_type": "single"})
    monkeypatch.setattr(main.loadConfig, "get_config", lambda supplied=None: config)
    with pytest.raises(SystemExit, match="trigger_type"):
        main.main(["--config", "stub.yaml"])


def test_main_refuses_an_empty_config_path(monkeypatch):
    monkeypatch.setattr(main.loadConfig, "_ACTIVE", None)
    with pytest.raises(SystemExit, match="--config is required"):
        main.main(["--config", ""])


def test_trigger_delay_from_message_uses_camera_id_list():
    payload = {
        "colour_1": ["trigger", 0.5],
        "depth_1": ["trigger", 0.0],
        "database_instruction": "search_database",
    }
    assert main.trigger_delay_from_message(payload, "colour_1") == 0.5
    assert main.trigger_delay_from_message(payload, "depth_1") == 0.0
    assert main.trigger_delay_from_message(payload, "other") is None


def test_trigger_delay_from_message_requires_delay_at_index_1():
    assert main.trigger_delay_from_message({"colour_1": "trigger"}, "colour_1") is None
    assert main.trigger_delay_from_message({"colour_1": ["trigger"]}, "colour_1") is None


def test_trigger_delay_from_message_ignores_other_camera_json_string():
    raw = (
        '{"colour_1": ["trigger", 0.5], "depth_1": ["trigger", 0.0], '
        '"lights_instruction": ["trigger", 0.5, 0.2]}'
    )
    assert main.trigger_delay_from_message(raw, "depth_1") == 0.0
    assert main.trigger_delay_from_message(raw, "colour_1") == 0.5
    assert main.trigger_delay_from_message(raw, "missing") is None
