import logging
import os
import time
import argparse
import signal
from sys import getsizeof
from queue import Empty, Queue
from threading import Event, Thread
import numpy as np
from pathlib import Path
import base64

from dependencies import loadConfig
from dependencies.CameraLibrary.cameras import Camera
from dependencies.CameraLibrary.hardware_trigger import CameraLossError
from dependencies.mqtt_functions import start_subscribe_thread
from dependencies.image_functions import (
    apply_image_format,
    build_image_topic,
    encode_date_time_to_bytes,
    encode_image_to_bytes,
    image_encoding,
    resolve_image_outputs,
)
from dependencies.archive_functions import archive_image
from mqtt_client import MQTTClient, MQTTConfig

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
    if key not in config:
        raise SystemExit(f"Missing required config key '{key}' in {loadConfig._CONFIG_PATH}")
    return config[key]


def set_camera_class(camera_type: str, config:dict):
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

def start_frame_thread(
        queue: Queue,
        camera: Camera,
        stop_event: Event,
        ) -> Thread:
    # Do not pass the wrapper as `camera=` — wait_for_frame expects the
    # vendor handle (self.cam). Omitting it lets Pylon/FLIR use self.cam.
    thread = Thread(
        target=camera.wait_for_frame,
        args=(queue, stop_event),
        daemon=True,
    )
    thread.start()
    return thread

def main(config_path: str | None = None) -> int:
    loadConfig.set_config_path(config_path)
    service_config = loadConfig.get_config()
    mqtt_config = loadConfig.return_config_value("mqtt")
    camera_config = loadConfig.return_config_value("camera")
    trigger_config = loadConfig.return_config_value("trigger")
    archive_config = loadConfig.return_config_value("archiving")
    image_outputs = resolve_image_outputs(service_config)
    base_image_topic = require(mqtt_config, "image_topic")

    logging_file = f'./logs/{require(camera_config, "camera_id")}_{require(camera_config, "camera_type")}_service_{time.strftime("%Y%m%d")}.log'

    os.makedirs(os.path.dirname(logging_file), exist_ok=True)
    if not os.path.exists(logging_file):
        with open(logging_file, "w") as file:
            file.write("")

    logging.basicConfig(
        filename=logging_file,
        level=logging.INFO,
        format='%(asctime)s - [PID %(process)d] - %(levelname)s - %(message)s',
        force=True,
        filemode='a'
    )

    camera = set_camera_class(require(camera_config, "camera_type"), camera_config)
    config = MQTTConfig(host=require(mqtt_config, "ip"), port=require(mqtt_config, "port"))
    client = MQTTClient(config)
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

    # GigE: hardware | software | continuous. LJS/others: external | internal.
    # internal/software → MQTT + capture_image; external/hardware/continuous (FLIR/dummy) → frame thread.
    _trigger = str(require(trigger_config, "trigger_type")).strip().lower()
    camera_type = str(require(camera_config, "camera_type")).strip().lower()
    is_frame_trigger = _trigger in ("external", "hardware") or (
        _trigger == "continuous" and camera_type in ("flir", "dummy")
    )

    if not is_frame_trigger:
        subscribe_thread = start_subscribe_thread(
            require(mqtt_config, "ip"),
            require(mqtt_config, "port"),
            require(mqtt_config, "trigger_topic"),
            event_queue,
            stop_event,
        )
    else:
        subscribe_thread = start_frame_thread(
            event_queue,
            camera,
            stop_event,
        )
    
    time.sleep(0.1)
    try:
        while not stop_event.is_set():
            
            try:
                message = event_queue.get(timeout = 1.0)
            except Empty:
                continue

            if isinstance(message, CameraLossError):
                logging.critical("CAMERA LOSS: %s", message)
                print(f"CAMERA LOSS: {message}", flush=True)
                exit_code = 1
                break

            if message is None:
                logging.info("Received invalid trigger payload; ignoring.")
                continue

            if is_frame_trigger:
                if not isinstance(message, np.ndarray):
                    logging.error("Expected image frame from queue, got %s", type(message))
                    continue
            elif "trigger" not in str(message):
                continue

            start_time = time.time()
            date_time = encode_date_time_to_bytes()

            logging.info("Capturing image...")
            if is_frame_trigger:
                image = message
            else:
                try:
                    image = camera.capture_image(timeout_ms=require(camera_config, "capture_timeout"))
                except Exception as e:
                    logging.error("Capture failed; skipping trigger: %s", e, exc_info=True)
                    continue

            if image is None:
                logging.error("No image available to encode.")
                continue

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            for output in image_outputs:
                try:
                    variant = apply_image_format(image, output["image_format"])
                except Exception as e:
                    logging.error(
                        "Failed to apply image_format for image %s: %s",
                        output["id"],
                        e,
                        exc_info=True,
                    )
                    continue

                topic = build_image_topic(base_image_topic, output["topic_end"])

                if require(archive_config, "is_archived") and output["archive"]:
                    archive_filename = (
                        f"cam_{require(camera_config, 'camera_id') or '0'}_"
                        f"{require(camera_config, 'camera_type')}_"
                        f"{output['id']}_{timestamp}"
                    )
                    archive_image(
                        variant,
                        require(archive_config, "archive_directory"),
                        archive_filename,
                        require(archive_config, "archive_parameters"),
                        require(camera_config, "camera_id"),
                    )

                image_bytes = encode_image_to_bytes(variant)
                packet = {
                    "image": base64.b64encode(image_bytes).decode("ascii"),
                    "date_time": date_time.decode("utf-8"),
                    "image_id": output["id"],
                    "encoding": image_encoding(variant),
                }

                logging.info(
                    "Publishing image %s to %s (size %s)",
                    output["id"],
                    topic,
                    getsizeof(image_bytes),
                )

                try:
                    client.publish(topic, packet)
                except Exception as e:
                    logging.error(
                        "Error publishing image %s to %s: %s",
                        output["id"],
                        topic,
                        e,
                    )

            print(f"imaging took a total of {time.time()-start_time}")
            logging.info("Image published. Waiting for next capture request...")

    except KeyboardInterrupt:
        logging.info("Shutting down and exiting.")

    finally:
        stop_event.set()
        # End acquisition first so a blocked GetNextImage unblocks and the
        # frame thread can exit before we DeInit (avoids leaving the camera locked).
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
    parser = argparse.ArgumentParser(description="Camera service")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Use fallback config (app/configs/config.yaml)",
    )
    args = parser.parse_args()

    if args.test and args.config:
        parser.error("cannot use both --test and --config")
    if not args.test and not args.config:
        parser.error("one of --config or --test is required")

    config_path = None if args.test else args.config
    raise SystemExit(main(config_path=config_path))