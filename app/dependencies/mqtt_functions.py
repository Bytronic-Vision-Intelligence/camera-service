from logging import info
import threading
from queue import Empty, Full, Queue

from mqtt_client import MQTTClient, MQTTConfig


def subscribe_listener(ip: str, port: int, trigger_topic: str, result_queue: Queue, stop_event: threading.Event):
    """Connect to a broker and feed every message on `trigger_topic` into a queue.

    When the queue is full (size-1 latest-only), the oldest payload is dropped
    so the main loop always sees the most recent inference result.

    Args:
        ip: broker address.
        port: broker port.
        trigger_topic: the topic to watch.
        result_queue: queue used to hand payloads back to the main thread.
        stop_event: shared shutdown signal (reserved; the client owns its loop).
    """
    del stop_event  # reserved for future cooperative shutdown
    config = MQTTConfig(host=ip, port=port)
    client = MQTTClient(config)
    client.connect()

    def _on_message(topic: str, payload: str) -> None:
        """Hand a received payload to the main thread, keeping only the latest."""
        info("Request received: %s", topic)
        try:
            result_queue.put_nowait(payload)
        except Full:
            try:
                result_queue.get_nowait()
            except Empty:
                pass
            try:
                result_queue.put_nowait(payload)
            except Full:
                pass

    client.subscribe(trigger_topic, _on_message)


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
