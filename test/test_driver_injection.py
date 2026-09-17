"""Drivers must be handed their settings -- they must not reach for globals."""

import ast
from pathlib import Path

LIBRARY = Path(__file__).resolve().parent.parent / "app/dependencies/CameraLibrary"


def test_no_driver_reads_configuration_from_anywhere():
    """Attribute injection only: no loadConfig / service_settings callbacks."""
    forbidden = {"loadConfig", "service_settings"}
    offenders = []

    for path in sorted(LIBRARY.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(
            path.read_text(encoding="utf-8", errors="replace"),
            filename=str(path),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dependencies":
                for alias in node.names:
                    if alias.name in forbidden:
                        offenders.append(
                            f"{path.name}:{node.lineno} imports {alias.name}"
                        )
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in forbidden
            ):
                offenders.append(
                    f"{path.name}:{node.lineno} reads {node.value.id}"
                )

    assert not offenders, (
        "these read config rather than being handed it: " + ", ".join(offenders)
    )
