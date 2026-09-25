import json
import signal
import time
from json import JSONDecodeError
from logging import critical, debug, error, info, warning
from queue import Empty, Queue
from threading import Event, Thread

import numpy as np
from mqtt_client import MQTTClient, MQTTConfig

from dependencies import loadConfig, logging_setup
from dependencies.CameraLibrary.cameras import Camera
from dependencies.CameraLibrary.hardware_trigger import CameraLossError
from dependencies.archive_functions import (
    archive_image,
    build_archive_filename,
    filename_categories,
    new_capture_id,
)
from dependencies.image_functions import (
    apply_image_format,
    build_image_topic,
    build_packet_list,
    encode_date_time_to_bytes,
    resolve_image_outputs,
)
from dependencies.mqtt_functions import start_subscribe_thread


def require(config: dict, key: str):
    """Return a required config value, or exit describing what is missing."""
    if key not in config or config[key] is None:
        try:
            where = f" in {loadConfig.config_path()}"
        except SystemExit:
            where = ""
        raise SystemExit(f"Missing required config key '{key}'{where}")
    return config[key]


def fill_placeholders(topic: str, values: dict) -> str:
    """Substitute ``{camera_id}`` and friends into a configured topic."""
    try:
        return topic.format_map(values)
    except KeyError as missing:
        raise SystemExit(
            f"Topic {topic!r} uses {missing} and nothing supplies it. "
            f"Available: {', '.join(sorted(values))}."
        )
    except (IndexError, ValueError) as exc:
        raise SystemExit(f"Topic {topic!r} is not a valid template: {exc}")


def topic_named(topics: list, name: str, values: dict | None = None) -> str:
    """Return the topic string declared under ``name``, placeholders filled in."""
    for topic in topics:
        if topic.get("name") != name:
            continue
        value = topic.get("topic")
        if not value:
            raise SystemExit(f"Topic '{name}' has no `topic:` value")
        return fill_placeholders(str(value), values or {})
    raise SystemExit(
        f"No topic named '{name}'. camera-service looks its "
        f"topics up by name; add `- name: {name}` under mqtt.topics."
    )


def topic_by_name(topics: list | None, name: str) -> str | None:
    """Return a named topic string, or None when it is not configured."""
    for topic in topics or []:
        if topic.get("name") == name:
            return topic.get("topic")
    return None


def apply_topic_placeholders(topics: list, values: dict) -> None:
    """Fill placeholders on every configured topic string in place."""
    for entry in topics:
        value = entry.get("topic")
        if value:
            entry["topic"] = fill_placeholders(str(value), values)


def set_camera_class(
    camera_type: str,
    camera_config: dict,
    trigger_config: dict,
    camera_settings: dict | None = None,
    lights_config: dict | None = None,
):
    """Construct a camera backend, inject service config, and connect.

    All backends take no constructor arguments; domain knobs are set on the
    instance before ``connect_to_camera`` so they never call back into
    ``loadConfig``.
    """
    if not camera_type:
        raise ValueError("Camera type cannot be empty.")

    if camera_type == "opencv":
        camera = Camera()
    elif camera_type == "dummy":
        from dependencies.CameraLibrary.cameras_dummy import DummyCamera
        camera = DummyCamera()
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

    camera.camera_config = camera_config
    camera.trigger_config = trigger_config
    camera.camera_settings = camera_settings or {}
    camera.lights_config = lights_config if isinstance(lights_config, dict) else {}
    camera.connect_to_camera()
    return camera


def start_frame_thread(queue: Queue, camera: Camera, stop_event: Event) -> Thread:
    """Run ``camera.wait_for_frame`` on a daemon thread (hardware paths)."""
    thread = Thread(
        target=camera.wait_for_frame,
        args=(queue, stop_event),
        daemon=True,
    )
    thread.start()
    return thread


def start_subscribers(mqtt_config: dict, topics: list, stop_event: Event) -> list:
    """Start one listener thread per subscribed topic (size-1 latest-only queues)."""
    threads = []
    for topic in topics:
        if not topic.get("is_subscribe"):
            continue
        topic["queue"] = Queue(maxsize=1)
        threads.append(
            start_subscribe_thread(
                mqtt_config["mqtt_ip"],
                mqtt_config["mqtt_port"],
                topic["topic"],
                topic["queue"],
                stop_event,
            )
        )
    return threads


def trigger_queue_from_topics(topics: list) -> Queue | None:
    """Return the size-1 queue for the named trigger topic, if any."""
    for topic in topics:
        if topic.get("name") == "trigger" and "queue" in topic:
            return topic["queue"]
    for topic in topics:
        if topic.get("is_trigger") and "queue" in topic:
            return topic["queue"]
    return None


def apply_sku_updates(topics: list, archive_config: dict) -> None:
    """Drain non-trigger sku_update messages into ``archive_config['selected_sku']``."""
    for topic in topics:
        if topic.get("name") != "sku_update" or "queue" not in topic:
            continue
        while True:
            try:
                payload = topic["queue"].get_nowait()
            except Empty:
                break
            data = _parse_mqtt_dict(payload)
            if not isinstance(data, dict):
                warning("Discarding non-object sku_update payload")
                continue
            sku = data.get("sku")
            if sku is None:
                sku = data.get("sku_id")
            if sku is None:
                warning("sku_update missing sku/sku_id; ignoring")
                continue
            sku_text = str(sku).strip()
            if not sku_text:
                continue
            archive_config["selected_sku"] = sku_text
            info("Archive selected_sku updated to %s", sku_text)


def continuous_interval_s(trigger_config: dict) -> float:
    """Seconds between software-continuous captures."""
    try:
        where = f" in {loadConfig.config_path()}"
    except SystemExit:
        where = ""
    try:
        delay = float(trigger_config.get("trigger_delay", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            f"Invalid service.trigger.trigger_delay{where}: {exc}"
        ) from exc
    if delay < 0:
        raise SystemExit(
            f"service.trigger.trigger_delay must be >= 0{where}"
        )
    if delay > 0:
        return delay
    try:
        fps = float(trigger_config.get("continuous_fps", 2) or 2)
    except (TypeError, ValueError):
        fps = 2.0
    return 1.0 / max(fps, 0.1)


def verdict_from_payload(message) -> str | None:
    """Read pass/fail from an inspection or analysis result message."""
    data = _parse_mqtt_dict(message)
    if not data:
        return None
    status = data.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower()
    verdict = data.get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        return verdict.strip().lower()
    if isinstance(verdict, dict):
        inner = verdict.get("verdict")
        if isinstance(inner, str) and inner.strip():
            return inner.strip().lower()
    return None


def _drain_queue(queue: Queue) -> None:
    while True:
        try:
            queue.get_nowait()
        except Empty:
            return


def verdict_timeout_s(archive_config: dict) -> float:
    """Seconds to wait for a result before archiving without a verdict."""
    params = archive_config.get("archive_parameters") or {}
    try:
        timeout = float(params.get("verdict_timeout_s", 30) or 30)
    except (TypeError, ValueError):
        timeout = 30.0
    return max(timeout, 0.0)


def wait_for_verdict(
    queue: Queue,
    timeout_s: float,
    stop_event: Event | None = None,
) -> str | None:
    """Return pass/fail, ``""`` if the message had none, or None on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        if stop_event is not None and stop_event.is_set():
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message = queue.get(timeout=min(remaining, 0.25))
        except Empty:
            continue
        verdict = verdict_from_payload(message)
        if verdict:
            return verdict
        warning("Result payload had no pass/fail verdict; archiving without one")
        return ""


def publish_outputs(
    client: MQTTClient,
    image,
    image_outputs: list,
    base_image_topic: str,
    camera_config: dict,
    archive_config: dict,
    topics: list | None = None,
    stop_event: Event | None = None,
    capture_id: str | None = None,
) -> None:
    """Format, publish, then archive. Archive waits for a result topic when set."""
    date_time = encode_date_time_to_bytes().decode("utf-8")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    capture_id = str(capture_id).strip() if capture_id else ""
    if not capture_id:
        capture_id = new_capture_id()
    archived = require(archive_config, "is_archived")
    is_archived = str(archived).strip().lower() in {"1", "true", "yes", "on"}

    prepared = []
    for output in image_outputs:
        try:
            variant = apply_image_format(image, output)
        except Exception as exc:
            error(
                "Failed to apply image_format for image %s: %s",
                output["id"],
                exc,
                exc_info=True,
            )
            continue
        prepared.append((output, variant))

    to_archive = [
        (output, variant)
        for output, variant in prepared
        if is_archived and output["archive"]
    ]
    categories = filename_categories(archive_config.get("archive_parameters"))
    wants_verdict = "verdict" in categories
    result_topic = topic_by_name(topics, "result") if wants_verdict else None
    result_queue = None
    if to_archive and wants_verdict and result_topic is None:
        warning("filename_categories includes verdict but no result topic is configured")
    if to_archive and result_topic is not None:
        for topic in topics or []:
            if topic.get("name") == "result":
                result_queue = topic.get("queue")
                break
        if result_queue is None:
            warning(
                "result topic %s is not subscribed; archiving without a verdict",
                result_topic,
            )
        else:
            # Drop a verdict from the previous capture before this image is published.
            _drain_queue(result_queue)

    sku_id = None
    if "sku_id" in categories:
        selected = archive_config.get("selected_sku")
        if selected is not None and str(selected).strip():
            sku_id = str(selected).strip()
        elif to_archive and topic_by_name(topics, "sku_update") is None:
            warning(
                "filename_categories includes sku_id but no sku_update topic "
                "or selected_sku is configured"
            )
        elif to_archive:
            warning("No sku selected yet; archiving without a sku")

    for output, variant in prepared:
        topic = build_image_topic(base_image_topic, output["topic_end"])
        packet_list = build_packet_list(
            topic,
            variant,
            image_id=output["id"],
            date_time=date_time,
            capture_id=capture_id,
        )
        info(
            "Publishing %s image packet(s) to %s",
            len(packet_list),
            topic,
        )
        try:
            client.publish_many(packet_list)
        except Exception as exc:
            error("Error publishing image %s to %s: %s", output["id"], topic, exc)

    verdict = None
    if to_archive and result_queue is not None:
        timeout_s = verdict_timeout_s(archive_config)
        info("Waiting up to %.1fs for a verdict on %s", timeout_s, result_topic)
        verdict = wait_for_verdict(result_queue, timeout_s, stop_event)
        if verdict == "":
            verdict = None
        elif verdict is None and (stop_event is None or not stop_event.is_set()):
            warning(
                "No verdict on %s within %.1fs; archiving without a verdict",
                result_topic,
                timeout_s,
            )

    for output, variant in to_archive:
        archive_filename = build_archive_filename(
            categories,
            {
                "camera_id": require(camera_config, "camera_id") or "0",
                "camera_type": require(camera_config, "camera_type"),
                "image_type": output["id"],
                "sku_id": sku_id,
                "datetime": timestamp,
                "verdict": verdict,
                "uuid": capture_id,
            },
        )
        archive_image(
            variant,
            require(archive_config, "archive_directory"),
            archive_filename,
            require(archive_config, "archive_parameters"),
            require(camera_config, "camera_id"),
        )


def _capture_and_publish(
    client,
    camera,
    camera_config,
    image_outputs,
    base_image_topic,
    archive_config,
    image=None,
    topics: list | None = None,
    stop_event: Event | None = None,
    capture_id: str | None = None,
) -> None:
    start_time = time.time()
    info("Capturing image...")
    if image is None:
        try:
            image = camera.capture_image(
                timeout_ms=require(camera_config, "capture_timeout")
            )
        except Exception as exc:
            error("Capture failed; skipping: %s", exc, exc_info=True)
            return
    if image is None:
        error("No image available to encode.")
        return
    publish_outputs(
        client,
        image,
        image_outputs,
        base_image_topic,
        camera_config,
        archive_config,
        topics,
        stop_event,
        capture_id,
    )
    info(
        "Image published in %.3fs.",
        time.time() - start_time,
    )


def _parse_mqtt_dict(message) -> dict | None:
    """Best-effort JSON object from an MQTT payload."""
    if isinstance(message, dict):
        return message
    text = ""
    if isinstance(message, (bytes, bytearray)):
        text = bytes(message).decode("utf-8", errors="replace")
    elif isinstance(message, str):
        text = message
    else:
        text = str(message) if message is not None else ""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def capture_id_from_message(message) -> str | None:
    """Read the shared capture id stamped on a trigger payload."""
    data = _parse_mqtt_dict(message)
    if not data:
        return None
    raw = data.get("capture_id")
    text = str(raw).strip() if raw is not None else ""
    return text or None


def trigger_delay_from_message(message, camera_id: str) -> float | None:
    """Return capture delay if ``message`` triggers this ``camera_id``, else None.

    Expects ``{camera_id: [trigger, delay, ...]}`` with delay at index 1.
    """
    data = _parse_mqtt_dict(message)
    if not data or not camera_id or camera_id not in data:
        return None

    value = data.get(camera_id)
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    if "trigger" not in str(value[0]).lower():
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def run_software_single(
    mqtt_config,
    topics,
    client,
    camera,
    camera_config,
    image_outputs,
    base_image_topic,
    archive_config,
    stop_event: Event,
) -> list:
    """MQTT trigger message → one software capture for this camera_id."""
    threads = start_subscribers(mqtt_config, topics, stop_event)
    event_queue = trigger_queue_from_topics(topics)
    if event_queue is None:
        raise SystemExit("software/single requires a subscribed trigger topic")

    camera_id = str(require(camera_config, "camera_id"))
    time.sleep(0.1)
    while not stop_event.is_set():
        apply_sku_updates(topics, archive_config)
        try:
            message = event_queue.get(timeout=1.0)
        except Empty:
            continue
        delay = trigger_delay_from_message(message, camera_id)
        if delay is None:
            continue
        if delay > 0:
            time.sleep(delay)
        _capture_and_publish(
            client, camera, camera_config, image_outputs,
            base_image_topic, archive_config,
            topics=topics, stop_event=stop_event,
            capture_id=capture_id_from_message(message),
        )
    return threads


def continuous_stream_command(message) -> bool | None:
    """Interpret an MQTT payload as continuous stream on/off.

    Returns True to start streaming, False to stop, None if the payload is
    not a recognised stream command.
    """
    text = ""
    data = None
    if isinstance(message, (bytes, bytearray)):
        text = bytes(message).decode("utf-8", errors="replace")
    elif isinstance(message, str):
        text = message
    elif isinstance(message, dict):
        data = message
    else:
        text = str(message)

    if data is None and text:
        try:
            parsed = json.loads(text)
        except (JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed

    if isinstance(data, dict):
        if "active" in data:
            active = data.get("active")
            if isinstance(active, str):
                return active.strip().lower() not in {"false", "0", "off", "no"}
            return bool(active)
        command = str(data.get("command") or data.get("stream") or "").strip().lower()
        if command in {"on", "start", "continuous", "enable"}:
            return True
        if command in {"off", "stop", "disable"}:
            return False

    lowered = text.strip().lower()
    if lowered in {"on", "start", "enable"}:
        return True
    if lowered in {"off", "stop", "disable"}:
        return False
    if '"active"' in lowered or "active" in lowered:
        # Fall through for ambiguous payloads.
        if "false" in lowered or "off" in lowered:
            return False
        if "true" in lowered or "on" in lowered:
            return True
    return None


def trigger_on_startup_enabled(trigger_config: dict) -> bool:
    """Return True when ``service.trigger.trigger_on_startup`` is truthy."""
    value = trigger_config.get("trigger_on_startup", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def run_software_continuous(
    mqtt_config,
    topics,
    trigger_config,
    client,
    camera,
    camera_config,
    image_outputs,
    base_image_topic,
    archive_config,
    stop_event: Event,
) -> list:
    """MQTT on/off controls a continuous software capture stream.

    Start with ``{"active": true}`` / ``on``; stop with ``{"active": false}`` /
    ``off``. While on, frames are captured at ``trigger_delay`` /
    ``continuous_fps`` — the service does not fire repeated trigger messages.

    When ``trigger_on_startup`` is true, streaming begins immediately without
    waiting for an MQTT arm command (MQTT off/on still stops and restarts it).
    """
    threads = start_subscribers(mqtt_config, topics, stop_event)
    event_queue = trigger_queue_from_topics(topics)
    if event_queue is None:
        raise SystemExit("software/continuous requires a subscribed trigger topic")

    interval = continuous_interval_s(trigger_config)
    streaming = trigger_on_startup_enabled(trigger_config)
    if streaming:
        info(
            "software/continuous stream ON (trigger_on_startup); "
            "capturing every %.3fs",
            interval,
        )
    else:
        info(
            "software/continuous idle; MQTT on/off arms capture every %.3fs",
            interval,
        )

    time.sleep(0.1)
    while not stop_event.is_set():
        apply_sku_updates(topics, archive_config)
        try:
            message = event_queue.get(timeout=0.1 if streaming else 1.0)
        except Empty:
            message = None

        if message is not None:
            command = continuous_stream_command(message)
            if command is True:
                streaming = True
                info("software/continuous stream ON")
            elif command is False:
                streaming = False
                info("software/continuous stream OFF")

        if not streaming:
            continue

        _capture_and_publish(
            client, camera, camera_config, image_outputs,
            base_image_topic, archive_config,
            topics=topics, stop_event=stop_event,
        )
        # Drain any stop command that arrived during capture; otherwise wait.
        deadline = time.monotonic() + interval
        while streaming and not stop_event.is_set() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                message = event_queue.get(timeout=max(remaining, 0.01))
            except Empty:
                break
            command = continuous_stream_command(message)
            if command is True:
                streaming = True
            elif command is False:
                streaming = False
                info("software/continuous stream OFF")
    return threads


def run_hardware_path(
    mqtt_config,
    topics,
    client,
    camera,
    camera_config,
    image_outputs,
    base_image_topic,
    archive_config,
    stop_event: Event,
    *,
    capture_type: str,
) -> list:
    """Hardware single (line/edge) or continuous (camera free-run stream).

    Both use ``wait_for_frame``; backends arm GenICam from injected trigger config.
    Subscribed topics (including optional ``result``) are listened to as well.
    """
    threads = start_subscribers(mqtt_config, topics, stop_event)
    event_queue: Queue = Queue(maxsize=1)
    threads.append(start_frame_thread(event_queue, camera, stop_event))
    info("hardware/%s waiting on camera frames", capture_type)

    time.sleep(0.1)
    while not stop_event.is_set():
        apply_sku_updates(topics, archive_config)
        try:
            message = event_queue.get(timeout=1.0)
        except Empty:
            continue

        if isinstance(message, CameraLossError):
            critical("CAMERA LOSS: %s", message)
            raise SystemExit(1)

        if not isinstance(message, np.ndarray):
            error("Expected image frame from queue, got %s", type(message))
            continue

        _capture_and_publish(
            client, camera, camera_config, image_outputs,
            base_image_topic, archive_config, image=message,
            topics=topics, stop_event=stop_event,
        )
    return threads


def main(argv=None) -> int:
    args = loadConfig.parse_cli(argv)
    config = loadConfig.get_config(args.config)

    log_settings = config.get("logging") or {}
    logging_setup.configure(log_settings.get("level", logging_setup.DEFAULT_LEVEL))

    mqtt_config = require(config, "mqtt")
    topics = require(mqtt_config, "topics")
    service = require(config, "service")

    camera_config = require(service, "camera")
    trigger_config = require(service, "trigger")
    archive_config = require(service, "archiving")
    # Live SKU stamped into archive filenames; updated via mqtt topic name sku_update.
    selected = archive_config.get("selected_sku")
    if selected is None:
        selected = service.get("selected_sku")
    if selected is not None and str(selected).strip():
        archive_config["selected_sku"] = str(selected).strip()
    camera_settings = service.get("camera_settings") or {}
    lights_config = service.get("lights") or {}
    image_outputs = resolve_image_outputs(service)

    placeholders = {
        "camera_id": require(camera_config, "camera_id"),
        "project": config.get("project", "project"),
    }
    apply_topic_placeholders(topics, placeholders)
    base_image_topic = topic_named(topics, "image", placeholders)

    trigger_type = str(require(trigger_config, "trigger_type")).strip().lower()
    capture_type = str(require(trigger_config, "capture_type")).strip().lower()
    try:
        where = f" in {loadConfig.config_path()}"
    except SystemExit:
        where = ""
    if trigger_type not in ("software", "hardware"):
        raise SystemExit(
            f"service.trigger.trigger_type must be 'software' or 'hardware' "
            f"(got {trigger_type!r}){where}"
        )
    if capture_type not in ("single", "continuous"):
        raise SystemExit(
            f"service.trigger.capture_type must be 'single' or 'continuous' "
            f"(got {capture_type!r}){where}"
        )

    camera = set_camera_class(
        require(camera_config, "camera_type"),
        camera_config,
        trigger_config,
        camera_settings=camera_settings,
        lights_config=lights_config,
    )
    client = MQTTClient(
        MQTTConfig(host=mqtt_config["mqtt_ip"], port=mqtt_config["mqtt_port"]))
    client.connect()

    stop_event = Event()
    threads: list = []
    exit_code = 0

    def _request_shutdown(signum, _frame):
        info("Received signal %s; requesting shutdown.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    try:
        if trigger_type == "software" and capture_type == "single":
            threads = run_software_single(
                mqtt_config, topics, client, camera, camera_config,
                image_outputs, base_image_topic, archive_config, stop_event,
            )
        elif trigger_type == "software" and capture_type == "continuous":
            threads = run_software_continuous(
                mqtt_config, topics, trigger_config, client, camera, camera_config,
                image_outputs, base_image_topic, archive_config, stop_event,
            )
        else:
            threads = run_hardware_path(
                mqtt_config, topics, client, camera, camera_config, image_outputs,
                base_image_topic, archive_config, stop_event,
                capture_type=capture_type,
            )
    except KeyboardInterrupt:
        info("Shutting down and exiting.")
    finally:
        stop_event.set()
        try:
            if hasattr(camera, "stop_acquisition"):
                camera.stop_acquisition()
        except Exception:
            debug("stop_acquisition during shutdown failed", exc_info=True)

        for thread in threads:
            if thread is not None and thread.is_alive():
                thread.join(timeout=2)

        camera.disconnect_camera(camera.cam)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
