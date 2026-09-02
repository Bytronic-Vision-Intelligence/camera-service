from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "config.yaml"
_CONFIG_PATH: Path | None = None


def set_config_path(path: str | Path | None) -> None:
    """Set the config file path for this process. None selects the default fallback."""
    global _CONFIG_PATH
    _CONFIG_PATH = _DEFAULT_CONFIG_PATH if path is None else Path(path).resolve()


def get_config() -> dict:
    """Read and return configuration from the configured YAML file.

    Returns an empty dict if the file is missing or empty.
    """
    if _CONFIG_PATH is None:
        raise RuntimeError("Config path not set; call set_config_path first")
    if not _CONFIG_PATH.exists():
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return config


def get_section(section: str) -> dict:
    """Return a top-level config subsection as a dict (empty dict if missing)."""
    if not section:
        raise ValueError("Section cannot be empty.")
    value = get_config().get(section)
    return value if isinstance(value, dict) else {}


def return_config_value(key: str) -> Any:
    """Return the value for a dot-separated key path in the loaded config.

    Examples: ``camera.camera_type``, ``archiving.archive_directory``.

    Raises ValueError for empty keys and KeyError when the path is missing.
    """
    if not key:
        raise ValueError("Key cannot be empty.")

    config = get_config()
    current: Any = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"Key '{key}' not found in configuration.")
        current = current[part]
    return current
