"""Shared broker fakes, so the suite runs without an MQTT broker."""


class FakeMQTTConfig:
    def __init__(self, host, port):
        self.host = host
        self.port = port


class FakeMQTTClient:
    """Delivers one payload straight back through subscribe(), no network."""

    def __init__(self, config, payload='{"command": "run"}'):
        self.config = config
        self.connected = False
        self.subscriptions = []
        self.published = []
        self.payload = payload

    def connect(self):
        self.connected = True

    def subscribe(self, topic, callback):
        self.subscriptions.append((topic, callback))
        callback(topic, self.payload)

    def publish(self, topic, message):
        self.published.append((topic, message))


class FakeThread:
    def __init__(self):
        self.join_called = False
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_called = True
        self.alive = False


class FakeCamera:
    """A camera driver that captures without hardware.

    Records what it was asked for, so a test can tell a capture that happened
    from one that was skipped.
    """

    def __init__(self, frame=None, capture_error=None):
        self.cam = object()
        self.frame = frame
        self.capture_error = capture_error
        self.connected = False
        self.disconnected = False
        self.captures = []

    def connect_to_camera(self):
        self.connected = True

    def capture_image(self, timeout_ms=None):
        self.captures.append(timeout_ms)
        if self.capture_error is not None:
            raise self.capture_error
        return self.frame

    def disconnect_camera(self, camera=None):
        self.disconnected = True
