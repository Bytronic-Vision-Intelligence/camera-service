from mqtt_client import MQTTClient, MQTTConfig
import threading
from queue import Queue
from json import loads
from logging import info
from base64 import b64decode
from numpy import ndarray, uint16, frombuffer, uint8
from cv2 import imencode, imdecode, IMREAD_UNCHANGED

def start_subscribe_thread(ip: str, port: int, topic: str, queue: Queue, stop_event: threading.Event) -> threading.Thread:
    """Run `subscribe_listener` on a daemon thread.

    Args:
        ip: broker address.
        port: broker port.
        topic: the topic to watch.
        queue: queue used to hand payloads back to the main thread.
        stop_event: shared shutdown signal.
    Returns:
        thread: the started daemon thread.
    """
    thread = threading.Thread(
        target=subscribe_listener,
        args=(ip, port, topic, queue, stop_event),
        daemon=True,
    )
    thread.start()
    return thread

def subscribe_listener(ip: str, port: int, trigger_topic: str, result_queue: Queue, stop_event: threading.Event):
    """Connect to a broker and feed every message on `trigger_topic` into a queue.

    Args:
        ip: broker address.
        port: broker port.
        trigger_topic: the topic to watch.
        result_queue: queue used to hand payloads back to the main thread.
        stop_event: shared shutdown signal (reserved; the client owns its loop).
    """
    config = MQTTConfig(host=ip, port=port)
    client = MQTTClient(config)
    client.connect()

    def _on_message(topic: str, payload: str) -> None:
        """Hand a received payload to the main thread."""
        info("Request received:", topic)
        result_queue.put(payload)

    client.subscribe(trigger_topic, _on_message)

def packetise_results(results:list[dict], topic:str):
    '''splits results into packets and then wraps then appropriately for publication
    Args:
        results: a ditionary of results
        results_map: a list of strings containing relevent headings
    Returns:
        a list of dictionaries containing the topic to publish on and the data to publish
    '''
    packet_list = []

    for packet_number in range(0, len(results)):
        packet = dict()
        packet["topic"] = f"{topic}/{packet_number}"
        packet["payload"] = {
            "packet_number": packet_number,
        }
        for item in results[packet_number]:
            packet["payload"][item] = results[packet_number].get(item)
        packet_list.append(packet)
    return packet_list

def encode_image_to_bytes(image: ndarray) -> bytes:
    """Encode the image as JPEG (8-bit) or PNG (uint16)."""
    if image.dtype == uint16:
        success, encoded_image = imencode(".png", image)
        if not success:
            raise RuntimeError("Failed to encode image to PNG format.")
        return encoded_image.tobytes()

def extract_image(encoded_image: str) -> bytes:
    """Decode base64 image bytes from a camera packet."""
    encoded = str(encoded_image).strip()
    if encoded.lower().startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    encoded = "".join(encoded.split())
    padding = (-len(encoded)) % 4
    if padding:
        encoded += "=" * padding
    return b64decode(encoded, validate=False)

def decode_image_from_bytes(data: bytes) -> ndarray:
    """Decode image bytes into an ndarray."""
    if not data:
        raise ValueError("Empty image bytes.")

    image = imdecode(frombuffer(data, uint8), IMREAD_UNCHANGED).astype('uint8')
    if image is None:
        raise ValueError("Could not decode image bytes.")
    return image