"""Read the configuration a service was told to run with.

``--config PATH`` is required. There is no default and no fallback: a service
runs only when something has said which configuration it is. That something is
service-orchestrator, which writes each instance's file and launches the binary
pointed at it, so one binary can serve several instances.

The absence of a fallback is the point. A service that quietly found a config
beside itself would start whenever one happened to be there -- a stale copy
from a previous deployment, the example shipped in the package, or the file
belonging to a different instance sharing the directory. It would come up,
subscribe, log success, and be the wrong service. Nothing would say so.

So the failures here are all the same failure, reported early: no --config, an
empty --config, or a path that is not a file.
"""

import argparse
import sys
from pathlib import Path

import yaml

#: The file in use this process, once resolved. Error messages read it, so a
#: service told to use one config never reports about another.
_ACTIVE: Path | None = None


def resolve_config_path(supplied) -> Path:
    """The config file to read.

    Args:
        supplied: the path given on the command line.
    Raises:
        SystemExit: when it is missing, empty or blank. An empty value reaches
            a service from an unset shell variable or a launcher that dropped
            an argument, and is a caller that meant to pass something.
    """
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
    """The config in use, for error messages.

    Raises:
        SystemExit: when nothing has been resolved yet, which means a caller
            reached for config before parsing arguments.
    """
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
    """The configuration this service was told to run with.

    Args:
        supplied: the path from --config. Omitted, the file already resolved
            for this process is re-read, so later reads never drift onto a
            different file.
    Raises:
        SystemExit: when no config has been named, or the named file is
            absent. Starting unconfigured is worse than not starting: the
            service comes up subscribed to nothing, publishing nowhere, and
            looks healthy to anything watching it.
    """
    path = config_path() if supplied is None and _ACTIVE else resolve_config_path(supplied)
    if not path.is_file():
        raise SystemExit(f"No such config file: {path}")
    return load_yaml(path)


def return_config_value(key: str):
    """One top-level value, or a KeyError naming the file it is missing from."""
    config = get_config()
    if key not in config:
        raise KeyError(f"Key '{key}' not found in {config_path()}")
    return config[key]


def parse_cli(argv=None) -> argparse.Namespace:
    """Parse the arguments a service accepts.

    --config is required, so a bare run exits 2 rather than guessing. --help
    still exits 0, which the release build depends on: it smoke-tests every
    binary by running it with --help and fails on a non-zero exit.
    """
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="A Bytronic service. Runs the configuration it is given; "
                    "service-orchestrator supplies it.")
    parser.add_argument(
        "--config", required=True, metavar="PATH",
        help="configuration to run with. Required: a service never looks for "
             "one on its own, so it cannot start the wrong instance.")
    return parser.parse_args(argv)
