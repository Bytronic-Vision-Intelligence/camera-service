"""Read this service's own settings out of the `service:` section.

loadConfig is deliberately small, and identical in every Bytronic service: it
reads the file it was told to read and hands back top-level keys. That is the
shared contract, and it stays the same everywhere.

A service's own settings are the opposite -- they belong to it alone, they
nest, and code deep inside a camera driver wants one of them without being
handed the whole configuration to dig through. This reads them, rooted at
`service:`, so `camera.serial_number` here means `service.camera.serial_number`
in the file. Drivers therefore keep the keys they already used when the config
was flat.

Nothing is cached. loadConfig re-reads the file it resolved for this process,
so a value read here is the value in the file this service was launched with,
and never one belonging to another instance sharing the directory.
"""

from dependencies import loadConfig


def settings() -> dict:
    """The `service:` section, or an empty mapping when it is absent.

    Empty rather than an error: main.py already refuses to start without the
    section, so by the time a driver calls this it exists. The fallback is for
    tests and for anything importing a driver outside a running service.
    """
    section = loadConfig.get_config().get("service")
    return section if isinstance(section, dict) else {}


def get_section(name: str) -> dict:
    """One named sub-section of `service:`, or an empty mapping.

    Args:
        name: the sub-section, e.g. "camera" or "trigger".
    Raises:
        ValueError: when `name` is empty, which is a caller bug rather than a
            configuration one.
    """
    if not name:
        raise ValueError("Section cannot be empty.")
    value = settings().get(name)
    return value if isinstance(value, dict) else {}


def return_config_value(key: str):
    """One value by dotted path, e.g. ``camera.serial_number``.

    Args:
        key: a dot-separated path beneath `service:`.
    Raises:
        ValueError: when `key` is empty.
        KeyError: when any part of the path is missing, naming the file it is
            missing from -- a service running one of several instance configs
            must not report about a file it is not using.
    """
    if not key:
        raise ValueError("Key cannot be empty.")

    current = settings()
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"Key 'service.{key}' not found in {loadConfig.config_path()}")
        current = current[part]
    return current
