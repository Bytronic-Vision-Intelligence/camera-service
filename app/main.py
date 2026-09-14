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

from dependencies import loadConfig
from dependencies.CameraLibrary.cameras import Camera
from dependencies.CameraLibrary.hardware_trigger import CameraLossError
from dependencies.mqtt_functions import start_subscribe_thread
from dependencies.data_functions import encode_date_time_to_bytes, encode_image_to_bytes
from dependencies.archive_functions import archive_image
from mqtt_client import MQTTClient, MQTTConfig

def load_runtime_config(config_path: str | None = None) -> dict:
    if config_path:
        loadConfig.set_config_path(config_path)

    ip = loadConfig.return_config_value("ip")
    port = loadConfig.return_config_value("port")
    trigger_topic = loadConfig.return_config_value("trigger_topic")
    trigger_time_topic = loadConfig.return_config_value("trigger_time_topic")
    message = loadConfig.return_config_value("message")

    camera_type = loadConfig.return_config_value("camera.camera_type")
    # Logical id for MQTT topics / archiving (ui_worker listens on camera_{id}).
    # Distinct from camera.serial_number, which selects the hardware device.
    try:
        camera_id = loadConfig.return_config_value("camera.camera_id")
    except KeyError:
        camera_id = None
    if camera_id is not None:
        camera_id = str(camera_id).strip() or None

    image_topic = loadConfig.return_config_value("image_topic")
    if camera_id:
        image_topic = image_topic.replace("/camera/", f"/camera_{camera_id}/")
    trigger_type = loadConfig.return_config_value("trigger.trigger_type")

    archive_directory = Path(loadConfig.return_config_value("archiving.archive_directory"))
    logging_file = f'./logs/{camera_type}_worker{time.strftime("%Y%m%d")}.log'
    buffer_size = loadConfig.get_section("camera_settings").get("buffer_size")
    capture_timeout_ms = int(loadConfig.get_section("camera_settings").get("capture_timeout_ms", 1000))
    is_archived = str(loadConfig.return_config_value("archiving.is_archived")).lower() == "true"
    archive_params = loadConfig.return_config_value("archiving.archive_parameters")

    return {
        "ip": ip,
        "port": port,
        "trigger_topic": trigger_topic,
        "trigger_time_topic": trigger_time_topic,
        "message": message,
        "camera_type": camera_type,
        "camera_id": camera_id,
        "image_topic": image_topic,
        "trigger_type": trigger_type,
        "archive_directory": archive_directory,
        "logging_file": logging_file,
        "buffer_size": buffer_size,
        "capture_timeout_ms": capture_timeout_ms,
        "is_archived": is_archived,
        "archive_params": archive_params,
    }

def set_camera_class(camera_type: str):
    if not camera_type:
        raise ValueError("Camera type cannot be empty.")
    
    if camera_type == "opencv":
        camera = Camera()
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
    cfg = load_runtime_config(config_path)
    ip = cfg["ip"]
    port = cfg["port"]
    trigger_topic = cfg["trigger_topic"]
    camera_type = cfg["camera_type"]
    camera_id = cfg["camera_id"]
    image_topic = cfg["image_topic"]
    trigger_type = cfg["trigger_type"]
    archive_directory = cfg["archive_directory"]
    logging_file = cfg["logging_file"]
    capture_timeout_ms = cfg["capture_timeout_ms"]
    is_archived = cfg["is_archived"]
    archive_params = cfg["archive_params"]

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

    camera = set_camera_class(camera_type)

    config = MQTTConfig(host=ip, port=port)
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
    # internal/software → MQTT + capture_image; external/hardware → frame thread.
    _trigger = str(trigger_type).strip().lower()
    is_external_trigger = _trigger in ("external", "hardware")

    if not is_external_trigger:
        subscribe_thread = start_subscribe_thread(
            ip,
            port,
            trigger_topic,
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
                msg = event_queue.get(timeout = 1.0)
                start_time = time.time()
            except Empty:
                continue

            if isinstance(msg, CameraLossError):
                logging.critical("CAMERA LOSS: %s", msg)
                print(f"CAMERA LOSS: {msg}", flush=True)
                exit_code = 1
                break

            if msg is None:
                logging.info("Received invalid trigger payload; ignoring.")
                continue

            date_time = encode_date_time_to_bytes()

            logging.info("Capturing image...")
            if not is_external_trigger:
                try:
                    image = camera.capture_image(timeout_ms=capture_timeout_ms)
                except Exception as e:
                    logging.error("Capture failed; skipping trigger: %s", e, exc_info=True)
                    continue
            else:
                if not isinstance(msg, np.ndarray):
                    logging.error("Expected image frame from queue, got %s", type(msg))
                    continue
                image = msg

            if image is None:
                logging.error("No image available to encode.")
                continue
            
            if is_archived:
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                archive_filename = f"cam{camera_id or '0'}_{camera_type}_{timestamp}"
                archive_image(image, archive_directory, archive_filename, archive_params, camera_id)

            image_bytes = encode_image_to_bytes(image)
            packet = image_bytes + date_time

            logging.info(f"Publishing image... of size {getsizeof(image_bytes)}")

            if image is not None:
                try:
                    client.publish(image_topic, packet)
                except Exception as e:
                    logging.error("Error publishing image: %s", e)
            else:
                logging.info("Failed to capture image.")

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
    parser = argparse.ArgumentParser(description="Camera worker")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file (defaults to app/Dependencies/config.yaml)",
    )
    args = parser.parse_args()
    raise SystemExit(main(config_path=args.config))