"""Reading a service's own settings out of the `service:` section.

The vendor drivers read their settings through this module rather than through
loadConfig, which stays identical in every Bytronic service. Before the
conversion they read top-level keys -- `camera.serial_number` meant the
top-level `camera:` section -- and the same dotted keys now resolve under
`service:` so the drivers themselves did not have to change.
"""

import pytest
import yaml

from dependencies import loadConfig, service_settings


@pytest.fixture
def config(tmp_path):
    def write(data):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        loadConfig.get_config(str(path))
        return path
    return write


def test_settings_returns_the_service_section(config):
    config({"service": {"camera": {"camera_type": "flir"}}, "mqtt": {"mqtt_ip": "x"}})

    assert service_settings.settings() == {"camera": {"camera_type": "flir"}}


def test_settings_is_empty_when_the_section_is_absent(config):
    """Drivers are importable outside a running service, and every caller here
    has a default. Raising would turn `import cameras_gige` into a crash."""
    config({"mqtt": {"mqtt_ip": "x"}})

    assert service_settings.settings() == {}


@pytest.mark.parametrize("written", [None, "camera", ["camera"], 3],
                         ids=["blank", "scalar", "list", "number"])
def test_settings_is_empty_when_the_section_is_not_a_mapping(config, written):
    """`service:` left blank parses as None, and `.get` on None raises. The
    other three are what a hand-edited config produces when someone writes the
    key and then puts the wrong thing under it -- `or {}` catches only the
    blank one, and lets a string through to be subscripted a frame away."""
    config({"service": written})

    assert service_settings.settings() == {}


def test_get_section_returns_a_named_subsection(config):
    config({"service": {"trigger": {"trigger_type": "external", "trigger_source": "Line0"}}})

    assert service_settings.get_section("trigger") == {
        "trigger_type": "external", "trigger_source": "Line0"}


def test_get_section_is_empty_when_the_subsection_is_missing(config):
    config({"service": {"camera": {}}})

    assert service_settings.get_section("camera_settings") == {}


def test_get_section_refuses_an_empty_name(config):
    """A caller bug, not a configuration one: no section is named "", so
    answering {} would hide the mistake behind a plausible default."""
    config({"service": {}})

    with pytest.raises(ValueError):
        service_settings.get_section("")


def test_return_config_value_walks_a_dotted_path(config):
    config({"service": {"camera": {"ljs": {"host": "192.168.0.1", "port": 24691}}}})

    assert service_settings.return_config_value("camera.ljs.host") == "192.168.0.1"
    assert service_settings.return_config_value("camera.ljs.port") == 24691


def test_the_dotted_path_is_rooted_at_the_service_section(config):
    """`camera.serial_number` is what the drivers ask for, and it must not find
    a top-level `camera:` left over from the old flat shape -- a stale section
    someone forgot to move would otherwise silently win."""
    config({
        "camera": {"serial_number": "STALE"},
        "service": {"camera": {"serial_number": "73400462"}},
    })

    assert service_settings.return_config_value("camera.serial_number") == "73400462"


def test_return_config_value_raises_naming_the_file(config):
    path = config({"service": {"camera": {}}})

    with pytest.raises(KeyError) as excinfo:
        service_settings.return_config_value("camera.serial_number")
    message = str(excinfo.value)
    assert "service.camera.serial_number" in message
    assert path.name in message


def test_return_config_value_raises_when_walking_through_a_scalar(config):
    config({"service": {"camera": {"camera_type": "flir"}}})

    with pytest.raises(KeyError):
        service_settings.return_config_value("camera.camera_type.nested")


def test_return_config_value_refuses_an_empty_key(config):
    config({"service": {}})

    with pytest.raises(ValueError):
        service_settings.return_config_value("")


def test_the_drivers_read_through_this_module_and_not_loadConfig():
    """loadConfig is copied verbatim from service-template into every service,
    so anything camera-specific added to it is a merge conflict waiting to
    happen -- and the first person to re-copy the file deletes it. Pinned here
    because the drivers would still work today either way."""
    import ast
    from pathlib import Path

    library = Path(__file__).resolve().parent.parent / "app/dependencies/CameraLibrary"
    offenders = []
    for path in sorted(library.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "loadConfig":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these reach into loadConfig, which is shared and copied verbatim: "
        + ", ".join(offenders))
