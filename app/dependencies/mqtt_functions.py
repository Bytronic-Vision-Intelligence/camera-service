import logging
from queue import Empty, Full, Queue
from threading import Event, Thread

from mqtt_client import MQTTClient, MQTTConfig


def start_subscribe_thread(
        ip: str,
        port: int,
        topic: str,
        queue: Queue,
        stop_event: Event
        ) -> Thread:
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
    thread = Thread(
        target=subscribe_listener,
        args=(ip, port, topic, queue, stop_event),
        daemon=True,
    )
    thread.start()
    return thread


def subscribe_listener(
        ip: str,
        port: int,
        trigger_topic: str,
        result_queue: Queue,
        stop_event: Event
        ):
    """Connect to a broker and feed messages on `trigger_topic` into a queue.

    Args:
        ip: broker address.
        port: broker port.
        trigger_topic: the topic to watch.
        result_queue: queue used to hand payloads back to the main thread.
        stop_event: shared shutdown signal; the listener returns when it is set.
    """
    config = MQTTConfig(host=ip, port=port)
    client = MQTTClient(config)
    client.connect()

    def on_message(topic: str, payload: str) -> None:
        # Handler signature used by mqtt_client.MQTTClient.subscribe
        logging.info("Request received: %s", topic)
        decoded = payload
        # Keep only the newest trigger to avoid replaying stale backlog bursts.
        # A camera that fell behind should take the picture being asked for
        # now, not work through the ones that were asked for while it was busy.
        try:
            result_queue.put_nowait(decoded)
        except Full:
            try:
                result_queue.get_nowait()
            except Empty:
                pass
            try:
                result_queue.put_nowait(decoded)
            except Full:
                # Another message won the race; skip this stale one.
                pass

    logging.info("Subscribing to %s", trigger_topic)
    client.subscribe(trigger_topic, on_message)
    stop_event.wait()
