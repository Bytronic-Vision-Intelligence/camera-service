"""camera-service: capture a frame when triggered, and publish it.

The service runs the configuration it is given -- `--config PATH`, required --
and takes its broker and topics from the `mqtt` section every Bytronic service
shares. Two topics are looked up by name:

  * `trigger`: a capture request arrives here. Subscribed to only when the
    camera is triggered by software. An externally triggered camera delivers
    frames itself, so there is nothing to subscribe to and no request to wait
    for.
  * `image`: the encoded frame is published here.

Its own settings -- which camera, how it triggers, whether frames are archived
-- live under `service`, and the vendor drivers read them from there through
dependencies.service_settings.
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
from dependencies.data_functions import encode_date_time_to_bytes, encode_image_to_bytes
from dependencies.mqtt_functions import start_subscribe_thread

#: trigger_type values that mean the camera delivers frames on its own.
EXTERNAL_TRIGGERS = ("external", "hardware")


def in_the_config() -> str:
    """" in <the config file>", or "" when none has been resolved.

    Named only if one has been loaded. These checks also run in contexts that
    never parsed arguments -- tests, and anything importing main -- and
    config_path() refuses to guess there, which would replace a message about
    the missing key with one about the wrong problem entirely.
    """
    try:
        return f" in {loadConfig.config_path()}"
    except SystemExit:
        return ""


def require(config: dict, key: str):
    """Return a required config value, or exit describing what is missing.

    Args:
        config: the mapping the key should be in.
        key: the key the service cannot start without.
    Returns:
        the value stored under `key`.
    Raises:
        SystemExit: when `key` is absent or empty, naming both the key and the
            file it is missing from.
    """
    # `is None` as well as absent: a key written and left blank is a section
    # somebody meant to fill in, and letting it through moves the failure to
    # whatever first subscripts it.
    if key not in config or config[key] is None:
        raise SystemExit(f"Missing required config key '{key}'{in_the_config()}")
    return config[key]


def topic_named(topics: list, name: str) -> str:
    """Return the topic string declared under `name`.

    Topics are matched by their `name`, not their position or their text, so a
    deployment can point this camera anywhere without the code knowing which
    of them is the trigger and which carries the image.

    Args:
        topics: the `mqtt.topics` entries.
        name: the `name:` to find.
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
                f"Topic '{name}' has no `topic:` value{in_the_config()}")
        return value
    raise SystemExit(
        f"No topic named '{name}'{in_the_config()}. camera-service looks its "
        f"topics up by name; add `- name: {name}` under mqtt.topics.")


def set_camera_class(camera_type: str, config: dict):
    """Construct and connect the driver for `camera_type`.

    Every driver but opencv is imported lazily: they need vendor SDKs that are
    not installed on a machine running a different camera, and a module-level
    import would stop the service starting at all.

    Args:
        camera_type: the `service.camera.camera_type` value.
        config: the `service.camera` section, for the settings a driver needs
            before it can be built.
    Raises:
        ValueError: when the type is empty or not one this service supports.
    """
    if not camera_type:
        raise ValueError("Camera type cannot be empty.")

    if camera_type == "opencv":
        camera = Camera()
    elif camera_type == "dummy":
        from dependencies.CameraLibrary.cameras_dummy import DummyCamera
        camera = DummyCamera(require(config, "dummy_location"), require(config, "file_type"))
    elif camera_type == "pylon":
        from dependencies.CameraLibrary.cameras_pylon import PylonCamera
        camera = PylonCamera()
    elif camera_type == "gige":
        from dependencies.CameraLibrary.cameras_gige import GigeCamera
        camera = GigeCamera()
    elif camera_type == "flir":
        from dependencies.CameraLibrary.cameras_flir import FlirCamera
        camera = FlirCamera()
    elif camera_type == "ljs":
        from dependencies.CameraLibrary.cameras_ljs import LJSCamera
        camera = LJSCamera()
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


def acquire_image(message, camera, camera_config: dict, is_external_trigger: bool):
    """Turn one queued message into an image, or None if there is nothing to do.

    The two trigger styles put entirely different things on the same queue, and
    conflating them is what broke this loop before: an MQTT payload is text
    asking for a picture, while an externally triggered camera puts the picture
    itself there.

    Args:
        message: whatever the listener or the frame thread queued.
        camera: the connected driver.
        camera_config: the `service.camera` section.
        is_external_trigger: whether the camera delivers its own frames.
    Returns:
        the image, or None when this message asked for nothing, was not a
        frame, or the grab failed. A failed grab is not fatal: the next trigger
        may well work, and taking the camera out of service over one bad frame
        loses every picture after it.
    """
    if is_external_trigger:
        if not isinstance(message, np.ndarray):
            logging.error("Expected an image frame from the queue, got %s", type(message))
            return None
        return message

    if not is_capture_request(message):
        return None

    logging.info("Capturing image...")
    try:
        image = camera.capture_image(timeout_ms=require(camera_config, "capture_timeout_ms"))
    except Exception as exc:
        logging.error("Capture failed; skipping trigger: %s", exc, exc_info=True)
        return None

    if image is None:
        # A driver that answered without raising and without a frame. Silence
        # here would look identical to a trigger nobody sent.
        logging.error("The camera returned no image to encode.")
    return image


def archive_if_wanted(image, camera_config: dict, archive_config: dict) -> None:
    """Write the frame to the archive, if this deployment keeps one.

    Args:
        image: the captured frame.
        camera_config: the `service.camera` section, for the name to file it
            under.
        archive_config: the `service.archiving` section.
    """
    if not require(archive_config, "is_archived"):
        return

    camera_id = require(camera_config, "camera_id")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"cam{camera_id or '0'}_{require(camera_config, 'camera_type')}_{timestamp}"
    archive_image(
        image,
        require(archive_config, "archive_directory"),
        filename,
        require(archive_config, "archive_params"),
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

    mqtt_config = require(config, "mqtt")
    topics = require(mqtt_config, "topics")
    service_config = require(config, "service")
    camera_config = require(service_config, "camera")
    archive_config = require(service_config, "archiving")

    broker_ip = require(mqtt_config, "mqtt_ip")
    broker_port = require(mqtt_config, "mqtt_port")

    # From `service.trigger`, which is where the drivers read it from too --
    # hardware_trigger.HardwareTriggerConfig and the GigE driver both take it
    # from that section. It used to be read from `service.camera` here and from
    # `service.trigger` there, so a config satisfying one of them broke the
    # other, and every vendor preset in this repository satisfied the drivers.
    #
    # GigE: hardware | software | continuous. LJS and others: external |
    # internal. internal/software -> MQTT trigger then capture_image;
    # external/hardware -> the camera's own frame thread.
    trigger_config = require(service_config, "trigger")
    trigger_type = str(require(trigger_config, "trigger_type")).strip().lower()
    is_external_trigger = trigger_type in EXTERNAL_TRIGGERS

    # Resolved before any hardware is opened, so a misnamed topic costs a
    # startup rather than a camera connection that is then thrown away.
    image_topic = topic_named(topics, "image")
    trigger_topic = None if is_external_trigger else topic_named(topics, "trigger")

    camera = set_camera_class(require(camera_config, "camera_type"), camera_config)
    client = MQTTClient(MQTTConfig(host=broker_ip, port=broker_port))
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

    if is_external_trigger:
        subscribe_thread = start_frame_thread(event_queue, camera, stop_event)
    else:
        subscribe_thread = start_subscribe_thread(
            broker_ip, broker_port, trigger_topic, event_queue, stop_event)

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

            image = acquire_image(message, camera, camera_config, is_external_trigger)
            if image is None:
                continue

            archive_if_wanted(image, camera_config, archive_config)

            image_bytes = encode_image_to_bytes(image)
            packet = {
                "image": base64.b64encode(image_bytes).decode("ascii"),
                "date_time": encode_date_time_to_bytes().decode("utf-8"),
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
