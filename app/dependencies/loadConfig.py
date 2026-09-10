"""Read the configuration a service was told to run with.

``--config PATH`` is required. There is no default and no fallback.

Domain settings under ``service:`` are loaded in ``main`` with ``require`` and
passed into camera backends when they are constructed. Cameras do not call
back into this module for pin/serial/trigger knobs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

_ACTIVE: Path | None = None


def resolve_config_path(supplied) -> Path:
    """The config file to read."""
    global _ACTIVE
    text = str(supplied or "").strip()
    if not text:
        raise SystemExit(
            "--config is required and must name a file. A service does not "
            "look for a config on its own: it runs the one it was told to, so "
            "it cannot start the wrong instance by finding a stale or "
            "example file beside it.")
    _ACTIVE = Path(text)
    return _ACTIVE


def config_path() -> Path:
    """The config in use, for error messages."""
    if _ACTIVE is None:
        raise SystemExit("no configuration has been loaded yet")
    return _ACTIVE


def load_yaml(path: Path) -> dict:
    """Read a YAML mapping, or an empty dict when there is nothing usable."""
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data if isinstance(data, dict) else {}


def get_config(supplied=None) -> dict:
    """The configuration this service was told to run with."""
    path = config_path() if supplied is None and _ACTIVE else resolve_config_path(supplied)
    if not path.is_file():
        raise SystemExit(f"No such config file: {path}")
    return load_yaml(path)


def parse_cli(argv=None) -> argparse.Namespace:
    """Parse the arguments a service accepts. ``--config`` is required."""
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="A Bytronic service. Runs the configuration it is given; "
                    "service-orchestrator supplies it.")
    parser.add_argument(
        "--config", required=True, metavar="PATH",
        help="configuration to run with. Required: a service never looks for "
             "one on its own, so it cannot start the wrong instance.")
    return parser.parse_args(argv)
