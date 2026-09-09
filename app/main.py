"""camera-service: capture a frame when triggered, and publish it.

The service runs the configuration it is given -- `--config PATH`, required --
and takes its broker and topics from the `mqtt` section every Bytronic service
shares. Two topics are looked up by name:

  * `trigger`: a capture request arrives here. Subscribed to only when the
    camera is triggered by software. An externally triggered camera delivers
    frames itself, so there is nothing to subscribe to and no request to wait
    for.
  * `image`: the encoded frame is published here.

Topics may carry `{camera_id}`, filled in from this service's own settings, so
one configuration serves any camera on the namespace.

Its own settings -- which camera, how it triggers, what to do to the frame,
whether it is archived -- live under `service`, and each vendor driver is
handed that section when it is built.
"""

import base64
import logging
import signal
import time
from queue import Empty, Queue
from sys import getsizeof
from threading import Event, Thread

import numpy as np
from mqtt_client import MQTTClient, MQTTConfig

from dependencies import loadConfig, logging_setup
from dependencies.CameraLibrary.cameras import Camera
from dependencies.CameraLibrary.hardware_trigger import CameraLossError
from dependencies.archive_functions import archive_image
from dependencies.image_functions import (
    apply_image_settings,
    encode_date_time_to_bytes,
    encode_image_to_bytes,
)
from dependencies.mqtt_functions import start_subscribe_thread

#: trigger_type values that mean the camera delivers frames on its own.
EXTERNAL_TRIGGERS = ("external", "hardware")


def require(config: dict, key: str):
    """Return a required top-level config value, or exit describing what is missing.

    Args:
        config: the loaded configuration mapping.
        key: the top-level key the service cannot start without.
    Returns:
        the value stored under `key`.
    Raises:
        SystemExit: when `key` is absent, naming both the key and the file.
    """
    # `is None` as well as absent: a key present but empty is a section
    # somebody meant to fill in, and letting it through moves the failure to
    # whatever first subscripts it.
    if key not in config or config[key] is None:
        # Named only if one has been loaded. require() is also called on
        # nested sections in contexts that never parsed arguments, and
        # config_path() refuses to guess there -- which would replace this
        # message with one about the wrong problem entirely.
        try:
            where = f" in {loadConfig.config_path()}"
        except SystemExit:
            where = ""
        raise SystemExit(f"Missing required config key '{key}'{where}")
    return config[key]


def fill_placeholders(topic: str, values: dict) -> str:
    """Substitute `{camera_id}` and friends into a configured topic.

    One configuration then serves any camera: the id is written once under
    `service.camera` and the topics follow it.

    Args:
        topic: the topic as configured, e.g. `project/camera/{camera_id}/image`.
        values: what the placeholders may refer to.
    Raises:
        SystemExit: naming a placeholder nothing can fill. Left in, it would be
            published and subscribed to literally -- the service would connect,
            report itself healthy, and match no camera at all.
    """
    try:
        return topic.format_map(values)
    except KeyError as missing:
        raise SystemExit(
            f"Topic {topic!r} uses {missing} and nothing supplies it. "
            f"Available: {', '.join(sorted(values))}.")
    except (IndexError, ValueError) as exc:
        raise SystemExit(f"Topic {topic!r} is not a valid template: {exc}")


def topic_named(topics: list, name: str, values: dict | None = None) -> str:
    """Return the topic string declared under `name`, placeholders filled in.

    Topics are matched by their `name`, not their position or their text, so a
    deployment can point this camera anywhere without the code knowing which
    of them is the trigger and which carries the image.

    Args:
        topics: the `mqtt.topics` entries.
        name: the `name:` to find.
        values: what `{placeholders}` in the topic may refer to.
    Raises:
        SystemExit: when no entry carries that name, or it carries no topic.
            Refusing here costs a startup; discovering it later means a camera
            that connects, reports itself healthy and publishes into nothing.
    """
    for topic in topics:
        if topic.get("name") != name:
            continue
        value = topic.get("topic")
        if not value:
            raise SystemExit(
                f"Topic '{name}' has no `topic:` value")
        return fill_placeholders(str(value), values or {})
    raise SystemExit(
        f"No topic named '{name}'. camera-service looks its "
        f"topics up by name; add `- name: {name}` under mqtt.topics.")


def read_settings(config: dict) -> dict:
    """Everything this service needs from its configuration, validated up front.

    All of it, before any hardware is opened. A `require` left in the capture
    loop is a configuration error that waits for a trigger to arrive -- on a
    machine nobody is watching, after the deployment was called good.

    Args:
        config: the whole configuration mapping.
    Returns:
        the settings main() runs on.
    Raises:
        SystemExit: naming the first key that is missing, and the file.
    """
    mqtt_config = require(config, "mqtt")
    topics = require(mqtt_config, "topics")
    service_config = require(config, "service")
    camera_config = require(service_config, "camera")
    trigger_config = require(service_config, "trigger")
    archive_config = require(service_config, "archiving")

    camera_id = require(camera_config, "camera_id")
    placeholders = {
        "camera_id": camera_id,
        "project": config.get("project", "project"),
    }

    # From `service.trigger`, which is where the drivers read it from too --
    # hardware_trigger.HardwareTriggerConfig and the GigE driver both take it
    # from that section.
    #
    # GigE: hardware | software | continuous. LJS and others: external |
    # internal. internal/software -> MQTT trigger then capture_image;
    # external/hardware -> the camera's own frame thread.
    trigger_type = str(require(trigger_config, "trigger_type")).strip().lower()
    is_external_trigger = trigger_type in EXTERNAL_TRIGGERS

    return {
        "broker_ip": require(mqtt_config, "mqtt_ip"),
        "broker_port": require(mqtt_config, "mqtt_port"),
        "image_topic": topic_named(topics, "image", placeholders),
        # Not looked up at all when nothing subscribes: an externally triggered
        # camera needs no trigger topic and must not be refused for lacking one.
        "trigger_topic": (None if is_external_trigger
                          else topic_named(topics, "trigger", placeholders)),
        "is_external_trigger": is_external_trigger,
        # The whole section, because a driver reads `camera`,
        # `camera_settings` and `trigger` from it.
        "service_config": service_config,
        "camera_config": camera_config,
        "camera_id": camera_id,
        "camera_type": require(camera_config, "camera_type"),
        "capture_timeout": require(camera_config, "capture_timeout"),
        # Optional, and applied only when present: `image: null` means leave
        # the frame exactly as the camera produced it.
        "image_config": service_config.get("image") or {},
        "is_archived": require(archive_config, "is_archived"),
        "archive_directory": require(archive_config, "archive_directory"),
        "archive_parameters": require(archive_config, "archive_parameters"),
    }


def set_camera_class(camera_type: str, settings: dict):
    """Construct and connect the driver for `camera_type`.

    Every driver but opencv is imported lazily: they need vendor SDKs that are
    not installed on a machine running a different camera, and a module-level
    import would stop the service starting at all.

    Each driver is HANDED its settings rather than reading them. A driver that
    reaches for a process-global config can only be exercised with a config
    file on disk -- which is why the vendor drivers had no tests at all -- and
    cannot be told about two cameras in one process.

    Args:
        camera_type: the `service.camera.camera_type` value.
        settings: the whole `service:` section. Drivers read `camera`,
            `camera_settings` and `trigger` from within it.
    Raises:
        ValueError: when the type is empty or not one this service supports.
    """
    if not camera_type:
        raise ValueError("Camera type cannot be empty.")

    camera_settings = settings.get("camera") or {}

    if camera_type == "opencv":
        camera = Camera()
    elif camera_type == "dummy":
        from dependencies.CameraLibrary.cameras_dummy import DummyCamera
        camera = DummyCamera(require(camera_settings, "dummy_location"),
                             require(camera_settings, "file_type"))
    elif camera_type == "pylon":
        from dependencies.CameraLibrary.cameras_pylon import PylonCamera
        camera = PylonCamera(settings)
    elif camera_type == "gige":
        from dependencies.CameraLibrary.cameras_gige import GigeCamera
        camera = GigeCamera(settings)
    elif camera_type == "flir":
        from dependencies.CameraLibrary.cameras_flir import FlirCamera
        camera = FlirCamera(settings)
    elif camera_type == "ljs":
        from dependencies.CameraLibrary.cameras_ljs import LJSCamera
        camera = LJSCamera(settings)
    else:
        raise ValueError(f"Unsupported camera type: {camera_type}")

    camera.connect_to_camera()
    return camera


def start_frame_thread(queue: Queue, camera: Camera, stop_event: Event) -> Thread:
    """Run the camera's own frame loop, for an externally triggered camera."""
    # Do not pass the wrapper as `camera=` -- wait_for_frame expects the
    # vendor handle (self.cam). Omitting it lets Pylon/FLIR use self.cam.
    thread = Thread(
        target=camera.wait_for_frame,
        args=(queue, stop_event),
        daemon=True,
    )
    thread.start()
    return thread


def is_capture_request(payload) -> bool:
    """Whether an MQTT payload asks for a capture.

    Other services publish on shared namespaces, so a message arriving on the
    trigger topic is not automatically a request to capture.
    """
    return "trigger" in str(payload)


def acquire_image(message, camera, settings: dict):
    """Turn one queued message into an image, or None if there is nothing to do.

    The two trigger styles put entirely different things on the same queue, and
    conflating them is what broke this loop before: an MQTT payload is text
    asking for a picture, while an externally triggered camera puts the picture
    itself there.

    Args:
        message: whatever the listener or the frame thread queued.
        camera: the connected driver.
        settings: what read_settings returned.
    Returns:
        the image, or None when this message asked for nothing, was not a
        frame, or the grab failed. A failed grab is not fatal: the next trigger
        may well work, and taking the camera out of service over one bad frame
        loses every picture after it.
    """
    if settings["is_external_trigger"]:
        if not isinstance(message, np.ndarray):
            logging.error("Expected an image frame from the queue, got %s", type(message))
            return None
        return message

    if not is_capture_request(message):
        return None

    logging.info("Capturing image...")
    try:
        image = camera.capture_image(timeout_ms=settings["capture_timeout"])
    except Exception as exc:
        logging.error("Capture failed; skipping trigger: %s", exc, exc_info=True)
        return None

    if image is None:
        # A driver that answered without raising and without a frame. Silence
        # here would look identical to a trigger nobody sent.
        logging.error("The camera returned no image to encode.")
    return image


def archive_if_wanted(image, settings: dict) -> None:
    """Write the frame to the archive, if this deployment keeps one."""
    if not settings["is_archived"]:
        return

    camera_id = settings["camera_id"]
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"cam_{camera_id or '0'}_{settings['camera_type']}_{timestamp}"
    archive_image(
        image,
        settings["archive_directory"],
        filename,
        settings["archive_parameters"],
        camera_id,
    )


def main(argv=None) -> int:
    args = loadConfig.parse_cli(argv)
    config = loadConfig.get_config(args.config)

    # Before the first thing that can exit. Until configure() runs the root
    # logger sits at WARNING and every info() is dropped, so a service that
    # failed while starting would say nothing about why.
    log_settings = config.get("logging") or {}
    logging_setup.configure(log_settings.get("level", logging_setup.DEFAULT_LEVEL))

    # All of it, before any hardware is opened: a misnamed topic then costs a
    # startup rather than a camera connection that is thrown away, and nothing
    # the loop needs can be missing once it is running.
    settings = read_settings(config)
    image_topic = settings["image_topic"]
    image_config = settings["image_config"]

    camera = set_camera_class(settings["camera_type"], settings["service_config"])
    client = MQTTClient(MQTTConfig(host=settings["broker_ip"], port=settings["broker_port"]))
    client.connect()

    # Latest-only queue: prevents long latency spikes from stale trigger backlog.
    event_queue = Queue(maxsize=1)
    stop_event = Event()
    exit_code = 0

    def _request_shutdown(signum, _frame):
        logging.info("Received signal %s; requesting shutdown.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    if settings["is_external_trigger"]:
        subscribe_thread = start_frame_thread(event_queue, camera, stop_event)
    else:
        subscribe_thread = start_subscribe_thread(
            settings["broker_ip"], settings["broker_port"],
            settings["trigger_topic"], event_queue, stop_event)

    time.sleep(0.1)
    try:
        while not stop_event.is_set():
            try:
                message = event_queue.get(timeout=1.0)
            except Empty:
                continue

            # First, before anything inspects the payload. A CameraLossError is
            # not a trigger and is not iterable, so testing it for a keyword
            # raises TypeError out of the loop instead of exiting non-zero as
            # intended.
            if isinstance(message, CameraLossError):
                logging.critical("CAMERA LOSS: %s", message)
                exit_code = 1
                break

            if message is None:
                logging.warning("Received an empty trigger payload; ignoring.")
                continue

            start_time = time.time()

            image = acquire_image(message, camera, settings)
            if image is None:
                continue

            date_time = encode_date_time_to_bytes()

            if image_config:
                image = apply_image_settings(image, image_config)

            archive_if_wanted(image, settings)

            image_bytes = encode_image_to_bytes(image)
            packet = {
                "image": base64.b64encode(image_bytes).decode("ascii"),
                "date_time": date_time.decode("utf-8"),
            }

            logging.info("Publishing image of size %s to %s", getsizeof(image_bytes), image_topic)
            try:
                client.publish(image_topic, packet)
            except Exception as exc:
                logging.error("Error publishing image: %s", exc)
                continue

            logging.info("Image published in %.3fs. Waiting for next capture request...",
                         time.time() - start_time)

    except KeyboardInterrupt:
        logging.info("Shutting down and exiting.")

    finally:
        stop_event.set()
        # End acquisition first so a blocked GetNextImage unblocks and the
        # frame thread can exit before we DeInit (avoids leaving the camera
        # locked).
        try:
            if hasattr(camera, "stop_acquisition"):
                camera.stop_acquisition()
        except Exception:
            logging.debug("stop_acquisition during shutdown failed", exc_info=True)

        if subscribe_thread is not None and subscribe_thread.is_alive():
            subscribe_thread.join(timeout=2)

        camera.disconnect_camera(camera.cam)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
