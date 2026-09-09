"""What camera-service does with the configuration it is given.

Several of these cover the paths that were broken before this service was
brought onto the template: the config shape main.py read did not exist in any
file in the repository, and neither of the two ways a frame reaches the loop
worked.
"""

from pathlib import Path
from queue import Empty, Queue

import numpy as np
import pytest
import yaml

from fakes import FakeCamera, FakeMQTTClient, FakeMQTTConfig, FakeThread

import main

CONFIG_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


def make_topics():
    """The shape shipped in config.example.yaml."""
    return [
        {"name": "trigger", "topic": "project/camera/colour_1/trigger",
         "is_subscribe": True, "is_trigger": True},
        {"name": "image", "topic": "project/camera/colour_1/image",
         "is_subscribe": False},
    ]


def make_config(**overrides):
    config = {
        "project": "project",
        "mqtt": {"mqtt_ip": "127.0.0.1", "mqtt_port": 1883, "topics": make_topics()},
        "service": {
            "camera": {"camera_type": "opencv", "camera_id": "colour_1",
                       "capture_timeout_ms": 5000},
            "trigger": {"trigger_type": "software"},
            "archiving": {"is_archived": False, "archive_directory": "save_dir",
                          "archive_params": {}},
        },
        "logging": {"level": "INFO"},
    }
    config.update(overrides)
    return config


def frame():
    return np.full((8, 8, 3), 128, dtype=np.uint8)


class StoppingQueue(Queue):
    """Delivers what a test queued, then ends the run.

    main owns the stop event and loops until it is set, so a test whose service
    never publishes has nothing to end it. Stopping on the first *empty* poll
    rather than at feed time is the point: a feed that set the event directly
    would set it before main reached its loop, and the loop body would never
    run at all -- so a test asserting "this message was ignored" would pass
    without the message ever being looked at.
    """

    stop_event = None

    def get(self, *args, **kwargs):
        if self.empty() and self.stop_event is not None:
            self.stop_event.set()
            raise Empty
        return super().get(*args, **kwargs)


@pytest.fixture
def run_main(monkeypatch):
    """Run main() against fakes, with the test choosing what reaches the queue.

    The loop exits on stop_event, which main owns, so a feed is handed the
    event and decides when there is nothing more coming. Without that the test
    would sit in `event_queue.get(timeout=1.0)` forever.
    """
    state = {"threads": [], "published": []}

    class RecordingClient(FakeMQTTClient):
        def publish(self, topic, message):
            super().publish(topic, message)
            state["published"].append((topic, message))

    def run(config, feed=None, camera=None):
        camera = camera if camera is not None else FakeCamera(frame=frame())
        state["camera"] = camera

        def default_feed(queue, stop_event):
            queue.put('{"trigger": true}')

        chosen = feed or default_feed

        def start_thread(queue, stop_event):
            state["stop_event"] = stop_event
            queue.stop_event = stop_event
            chosen(queue, stop_event)
            thread = FakeThread()
            state["threads"].append(thread)
            return thread

        monkeypatch.setattr(main, "Queue", StoppingQueue)
        monkeypatch.setattr(main, "MQTTConfig", FakeMQTTConfig)
        monkeypatch.setattr(
            main, "MQTTClient",
            lambda config_: state.setdefault("client", RecordingClient(config_)))
        monkeypatch.setattr(main, "set_camera_class", lambda camera_type, cfg: camera)
        monkeypatch.setattr(
            main, "start_subscribe_thread",
            lambda ip, port, topic, queue, stop_event: (
                state.__setitem__("subscribed_to", topic), start_thread(queue, stop_event))[1])
        monkeypatch.setattr(
            main, "start_frame_thread",
            lambda queue, camera_, stop_event: start_thread(queue, stop_event))
        monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(main.loadConfig, "get_config", lambda supplied=None: config)
        # configure() replaces the root handlers, and pytest's caplog handler
        # is one of them -- so a test asserting on what the loop reported would
        # read an empty log. What it configures is covered by the tests below
        # that call main() directly.
        monkeypatch.setattr(main.logging_setup, "configure",
                            lambda level=None: state.setdefault("log_levels", []).append(level))

        state["exit_code"] = main.main(["--config", "stub.yaml"])
        return state

    return run


# --- require -------------------------------------------------------------

def test_require_returns_the_value_when_present():
    assert main.require({"topics": []}, "topics") == []


def test_require_exits_naming_the_missing_key():
    with pytest.raises(SystemExit, match="mqtt"):
        main.require({}, "mqtt")


def test_a_section_present_but_empty_is_refused():
    """`archiving:` written and left blank is a section somebody meant to fill
    in. Letting it through moves the failure to whatever first subscripts it,
    a stack frame away from the config that caused it."""
    with pytest.raises(SystemExit, match="archiving"):
        main.require({"archiving": None}, "archiving")


# --- topic_named ---------------------------------------------------------

def test_topic_named_returns_the_topic_declared_under_that_name():
    assert main.topic_named(make_topics(), "image") == "project/camera/colour_1/image"


def test_topic_named_exits_when_no_entry_carries_the_name():
    """A camera whose image topic is missing would otherwise connect, report
    itself healthy and publish into nothing."""
    with pytest.raises(SystemExit, match="No topic named 'image'"):
        main.topic_named([t for t in make_topics() if t["name"] != "image"], "image")


def test_topic_named_exits_when_the_entry_has_no_topic():
    """`- name: image` with the topic line deleted is not the same as a topic
    named "": publishing to an empty string is accepted by the broker and read
    by nobody."""
    with pytest.raises(SystemExit, match="no `topic:` value"):
        main.topic_named([{"name": "image", "topic": ""}], "image")


# --- is_capture_request --------------------------------------------------

def test_a_trigger_payload_is_a_capture_request():
    assert main.is_capture_request('{"trigger": true}')


def test_another_service_message_on_the_topic_is_not():
    assert not main.is_capture_request('{"status": "ready"}')


def test_a_frame_is_never_mistaken_for_a_capture_request():
    """The external-trigger path puts numpy frames on the same queue. `"trigger"
    in <ndarray>` is an elementwise comparison that answers False, so the old
    membership test skipped every frame the camera captured -- the whole
    hardware-trigger path did nothing, silently."""
    assert not main.is_capture_request(frame())


# --- the loop ------------------------------------------------------------

def test_a_software_trigger_captures_and_publishes(run_main):
    state = run_main(make_config())

    assert state["camera"].captures == [5000], "capture_timeout_ms did not reach the driver"
    assert [topic for topic, _ in state["published"]] == ["project/camera/colour_1/image"]
    assert set(state["published"][0][1]) == {"image", "date_time"}
    assert state["exit_code"] == 0


def test_it_subscribes_to_the_topic_named_trigger(run_main):
    state = run_main(make_config())

    assert state["subscribed_to"] == "project/camera/colour_1/trigger"


def test_a_message_that_is_not_a_capture_request_captures_nothing(run_main):
    """Something else published on the trigger topic. The camera should ignore
    it rather than take a picture for it."""
    def feed(queue, stop_event):
        queue.put('{"status": "ready"}')

    state = run_main(make_config(), feed=feed)

    assert state["camera"].captures == []
    assert state["published"] == []


def test_a_capture_that_fails_skips_the_trigger_rather_than_stopping(run_main, caplog):
    """One bad grab is not a reason to take the camera out of service; the next
    trigger may well work."""
    camera = FakeCamera(capture_error=RuntimeError("grab timed out"))

    def feed(queue, stop_event):
        queue.put('{"trigger": true}')

    with caplog.at_level("ERROR"):
        state = run_main(make_config(), feed=feed, camera=camera)

    assert state["published"] == []
    assert "Capture failed" in caplog.text
    assert state["exit_code"] == 0


def test_a_driver_returning_no_frame_is_reported(run_main, caplog):
    """capture_image answering None without raising. Saying nothing would look
    exactly like a trigger nobody sent."""
    with caplog.at_level("ERROR"):
        state = run_main(make_config(), camera=FakeCamera(frame=None))

    assert state["published"] == []
    assert "returned no image" in caplog.text


def test_an_externally_triggered_camera_publishes_the_frame_it_is_given(run_main):
    """No MQTT trigger and no capture_image call: the camera's own frame thread
    delivers frames, and the frame IS the trigger.

    This path did nothing at all before. Frames were tested for the word
    "trigger", which on a numpy array is an elementwise comparison answering
    False, so every captured frame was silently discarded."""
    config = make_config()
    config["service"]["trigger"]["trigger_type"] = "hardware"

    state = run_main(config, feed=lambda queue, stop_event: queue.put(frame()))

    assert state["camera"].captures == [], "asked the camera to capture on an external trigger"
    assert [topic for topic, _ in state["published"]] == ["project/camera/colour_1/image"]


def test_a_camera_loss_exits_non_zero_without_publishing(run_main):
    """CameraLossError is not iterable, so testing it for the word "trigger"
    raised TypeError out of the loop -- a crash with a traceback instead of the
    clean exit(1) the branch was written to produce. It is now checked before
    anything inspects the payload."""
    from dependencies.CameraLibrary.hardware_trigger import CameraLossError

    config = make_config()
    config["service"]["trigger"]["trigger_type"] = "hardware"

    state = run_main(
        config,
        feed=lambda queue, stop_event: queue.put(CameraLossError("head unreachable")))

    assert state["exit_code"] == 1
    assert state["published"] == []


def test_the_frame_thread_delivering_something_else_is_reported_not_published(run_main, caplog):
    config = make_config()
    config["service"]["trigger"]["trigger_type"] = "external"

    def feed(queue, stop_event):
        queue.put("not a frame")

    with caplog.at_level("ERROR"):
        state = run_main(config, feed=feed)

    assert "Expected an image frame" in caplog.text
    assert state["published"] == []


def test_archiving_is_skipped_when_it_is_turned_off(run_main, monkeypatch):
    archived = []
    monkeypatch.setattr(main, "archive_image",
                        lambda *args, **kwargs: archived.append(args))

    run_main(make_config())

    assert archived == []


def test_archiving_names_the_file_after_the_camera(run_main, monkeypatch):
    archived = []
    monkeypatch.setattr(main, "archive_image",
                        lambda *args, **kwargs: archived.append(args))

    config = make_config()
    config["service"]["archiving"]["is_archived"] = True

    run_main(config)

    assert len(archived) == 1
    _image, directory, filename, _params, camera_id = archived[0]
    assert directory == "save_dir"
    assert filename.startswith("camcolour_1_opencv_")
    assert camera_id == "colour_1"


def test_the_camera_is_disconnected_even_when_the_loop_ends_badly(run_main):
    """A camera left connected is a camera the next process cannot open."""
    from dependencies.CameraLibrary.hardware_trigger import CameraLossError

    config = make_config()
    config["service"]["trigger"]["trigger_type"] = "hardware"

    state = run_main(
        config,
        feed=lambda queue, stop_event: queue.put(CameraLossError("head unreachable")))

    assert state["camera"].disconnected


# --- configuration -------------------------------------------------------

def test_main_answers_help_before_looking_for_a_config(monkeypatch):
    """--help must exit 0 on a binary that has no config beside it, which is
    every binary the release pipeline builds. Reaching get_config() first exits
    1 for want of a file that only exists once deployed, and no release could
    ever be published."""
    monkeypatch.setattr(main.loadConfig, "get_config",
                        lambda supplied=None: pytest.fail("looked for a config before --help"))
    with pytest.raises(SystemExit) as exit_info:
        main.main(["--help"])
    assert exit_info.value.code == 0


def test_main_refuses_an_empty_config_path():
    """`--config ""` reaches main from an unset shell variable or a launcher
    that dropped an argument. With no fallback there is nothing to quietly
    start instead, and the refusal says which flag is at fault."""
    with pytest.raises(SystemExit, match="--config is required"):
        main.main(["--config", ""])


def test_main_configures_logging_before_it_can_fail(monkeypatch):
    """Until configure() runs, the root logger sits at WARNING and every info()
    call is dropped. A service that failed while starting would then report
    nothing about why -- which is precisely when the log matters."""
    from dependencies import logging_setup

    order = []
    monkeypatch.setattr(logging_setup, "configure", lambda level=None: order.append("configured"))
    monkeypatch.setattr(main.loadConfig, "get_config", lambda supplied=None: {})

    with pytest.raises(SystemExit):
        main.main(["--config", "stub.yaml"])
    assert order == ["configured"], "logging was not configured before the first failure"


def test_the_configured_level_comes_from_the_service_config(monkeypatch):
    from dependencies import logging_setup

    seen = []
    monkeypatch.setattr(logging_setup, "configure", lambda level=None: seen.append(level))
    monkeypatch.setattr(main.loadConfig, "get_config",
                        lambda supplied=None: {"logging": {"level": "DEBUG"}})
    with pytest.raises(SystemExit):
        main.main(["--config", "stub.yaml"])
    assert seen == ["DEBUG"]


def test_a_config_without_a_logging_section_still_starts(monkeypatch):
    """Every config written before this setting existed lacks the section. A
    missing one must mean the default, not a crash on startup."""
    from dependencies import logging_setup

    seen = []
    monkeypatch.setattr(logging_setup, "configure", lambda level=None: seen.append(level))
    monkeypatch.setattr(main.loadConfig, "get_config", lambda supplied=None: {})
    with pytest.raises(SystemExit):
        main.main(["--config", "stub.yaml"])
    assert seen == [logging_setup.DEFAULT_LEVEL]


def test_the_config_shipped_in_the_repo_satisfies_what_main_requires():
    """config.example.yaml is what a customer receives beside the binary, and
    what the release workflow copies in. If it lacks a key main requires, every
    fresh install fails on first start.

    Before this conversion no file in this repository satisfied main.py at all:
    it read `mqtt.*` and every config here was flat."""
    config = yaml.safe_load(CONFIG_EXAMPLE.read_text(encoding="utf-8"))

    mqtt = main.require(config, "mqtt")
    topics = main.require(mqtt, "topics")
    service = main.require(config, "service")
    camera = main.require(service, "camera")
    main.require(service, "archiving")
    main.require(main.require(service, "trigger"), "trigger_type")
    main.require(camera, "camera_type")
    main.require(camera, "camera_id")
    main.require(camera, "capture_timeout_ms")

    for key in ("mqtt_ip", "mqtt_port"):
        assert key in mqtt, f"{key} missing from the shipped config"
    assert main.topic_named(topics, "trigger")
    assert main.topic_named(topics, "image")


@pytest.mark.parametrize("example", sorted(
    (Path(__file__).resolve().parent.parent / "docs/config-examples").glob("*.yaml")),
    ids=lambda path: path.stem)
def test_every_documented_example_satisfies_what_main_requires(example):
    """The per-camera examples are reference material, so nothing loads them and
    nothing else would notice them drifting. Every one of them disagreed with
    main.py before the conversion -- `trigger_type` in a section main never
    read, and `archive_parameters` where the code wants `archive_params`."""
    config = yaml.safe_load(example.read_text(encoding="utf-8"))

    mqtt = main.require(config, "mqtt")
    service = main.require(config, "service")
    main.require(main.require(service, "trigger"), "trigger_type")
    main.require(main.require(service, "archiving"), "archive_params")
    main.require(main.require(service, "camera"), "camera_type")
    assert main.topic_named(main.require(mqtt, "topics"), "image")


def test_main_runs_the_config_it_is_given(tmp_path, monkeypatch):
    """One binary, several cameras: the file named on the command line is the
    one that runs, and nothing else is consulted."""
    other = tmp_path / "config-2.yaml"
    other.write_text("mqtt:\n  mqtt_ip: 10.0.0.2\n")

    seen = {}
    monkeypatch.setattr(main, "require",
                        lambda config, key: seen.setdefault(key, config.get(key)) or {})
    # SystemExit, not Exception: it derives from BaseException, so the obvious
    # `pytest.raises(Exception)` here would let the exit escape the test.
    with pytest.raises(SystemExit):
        main.main(["--config", str(other)])
    assert seen["mqtt"]["mqtt_ip"] == "10.0.0.2"
