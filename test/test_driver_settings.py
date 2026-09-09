"""The vendor drivers read the settings they are HANDED, and nothing else.

Until this, none of them had a test. They reached into a process-global config
through a `service_settings` shim, so exercising one meant putting a config
file on disk and hoping the right global was set — and no test did, which is
how `trigger_type` came to be read from one section here and a different one in
main.py without anything noticing.

Handed their settings instead, they are ordinary objects: constructible, with
their configuration visible. PySpin ships only as a Windows wheel, so the two
modules that need it are imported against a stand-in; everything they are asked
here is Python, not Spinnaker.
"""

import ast
import sys
import types
from pathlib import Path

import pytest

LIBRARY = Path(__file__).resolve().parent.parent / "app/dependencies/CameraLibrary"


def service_section():
    """A `service:` section as service-orchestrator would write one."""
    return {
        "camera": {
            "camera_type": "gige",
            "camera_id": "colour_1",
            "serial_number": "25165090",
            "gentl_cti": "/opt/vendor/producer.cti",
            "ljs": {"host": "10.0.0.9", "port": 24691, "high_speed_port": 24692,
                    "program": 3, "settings": {"exposure_time_us": 640}},
        },
        "camera_settings": {"pixel_format": "Mono14"},
        "trigger": {"trigger_type": "external", "trigger_source": "Line2",
                    "trigger_activation": "FallingEdge"},
    }


@pytest.fixture
def spinnaker_stand_in(monkeypatch):
    """Import the FLIR modules without the Windows-only Spinnaker SDK.

    Nothing here calls into it: the FLIR driver touches PySpin inside its
    connect and capture paths, and what is under test is the configuration it
    reads before reaching them.
    """
    module = types.ModuleType("PySpin")
    monkeypatch.setitem(sys.modules, "PySpin", module)
    for name in list(sys.modules):
        if name.endswith(("cameras_flir", "spinnaker_trigger")):
            monkeypatch.delitem(sys.modules, name, raising=False)
    yield module


# --- each driver keeps what it was given ---------------------------------

def test_the_gige_driver_reads_the_settings_it_is_handed():
    from dependencies.CameraLibrary.cameras_gige import GigeCamera

    camera = GigeCamera(service_section())

    assert camera.settings["camera"]["serial_number"] == "25165090"
    assert camera.settings["camera_settings"]["pixel_format"] == "Mono14"
    assert camera.settings["trigger"]["trigger_source"] == "Line2"


def test_the_pylon_driver_reads_the_settings_it_is_handed():
    from dependencies.CameraLibrary.cameras_pylon import PylonCamera

    camera = PylonCamera(service_section())

    assert camera.settings["camera"]["serial_number"] == "25165090"


def test_the_flir_driver_reads_the_settings_it_is_handed(spinnaker_stand_in):
    from dependencies.CameraLibrary.cameras_flir import FlirCamera

    camera = FlirCamera(service_section())

    assert camera.settings["camera_settings"]["pixel_format"] == "Mono14"


@pytest.mark.parametrize("driver", ["cameras_gige", "cameras_pylon"])
def test_a_driver_built_with_no_settings_still_constructs(driver):
    """Importing or constructing a driver must not need a configuration.

    A settings-less driver falls back to "first device found", which is the
    right behaviour for a bench script and the wrong one for a deployment --
    so it must be possible, and main.py must never do it."""
    import importlib

    module = importlib.import_module(f"dependencies.CameraLibrary.{driver}")
    camera = next(getattr(module, name) for name in dir(module)
                  if name.endswith("Camera") and name != "Camera")()

    assert camera.settings == {}


# --- the LJ-S head -------------------------------------------------------

def test_the_ljs_head_reads_its_own_subsection():
    from dependencies.CameraLibrary.cameras_ljs import LJSCamera

    head = LJSCamera(service_section())

    assert head.host == "10.0.0.9"
    assert head.program == 3


def test_ljs_overrides_beat_the_configuration():
    """A caller driving the head directly, rather than through a service."""
    from dependencies.CameraLibrary.cameras_ljs import LJSCamera

    head = LJSCamera(service_section(), host="192.168.0.1")

    assert head.host == "192.168.0.1"


def test_ljs_settings_are_the_program_settings_not_the_service_section():
    """`self.settings` on this class means the LJ-S program registry, and it is
    assigned at the end of __init__. Storing the service section under the same
    name gives one attribute two meanings, and the second assignment wins."""
    from dependencies.CameraLibrary.cameras_ljs import LJSCamera

    head = LJSCamera(service_section())

    assert head.settings == {"exposure_time_us": 640}


def test_ljs_refuses_two_identical_ports():
    from dependencies.CameraLibrary.cameras_ljs import LJSCamera

    settings = service_section()
    settings["camera"]["ljs"]["high_speed_port"] = 24691

    with pytest.raises(ValueError, match="must differ"):
        LJSCamera(settings)


# --- the hardware trigger ------------------------------------------------

def test_the_trigger_config_is_built_from_the_section_it_is_given():
    from dependencies.CameraLibrary.hardware_trigger import HardwareTriggerConfig

    config = HardwareTriggerConfig.from_trigger_settings(service_section()["trigger"])

    assert config.enabled is True
    assert config.source == "Line2"
    assert config.activation == "FallingEdge"


def test_a_software_trigger_is_not_a_hardware_one():
    from dependencies.CameraLibrary.hardware_trigger import HardwareTriggerConfig

    config = HardwareTriggerConfig.from_trigger_settings({"trigger_type": "software"})

    assert config.enabled is False


def test_the_trigger_config_needs_a_section_and_will_not_find_one():
    """It used to fall back to reading the process-global config, so a caller
    that had configured nothing got a trigger anyway and could not tell."""
    from dependencies.CameraLibrary.hardware_trigger import HardwareTriggerConfig

    with pytest.raises(TypeError):
        HardwareTriggerConfig.from_trigger_settings()


def test_the_spinnaker_trigger_will_not_invent_a_config(spinnaker_stand_in):
    from dependencies.CameraLibrary.spinnaker_trigger import SpinnakerHardwareTrigger

    with pytest.raises(TypeError):
        SpinnakerHardwareTrigger(object())


# --- the habit itself ----------------------------------------------------

def test_no_driver_reads_configuration_from_anywhere():
    """The whole point. A driver that reaches for a global can only be
    exercised with a config file on disk, cannot be told about two cameras in
    one process, and drifts away from main.py without anything noticing --
    which is exactly what happened to `trigger_type`."""
    forbidden = {"loadConfig", "service_settings"}
    offenders = []

    for path in sorted(LIBRARY.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dependencies":
                for alias in node.names:
                    if alias.name in forbidden:
                        offenders.append(f"{path.name}:{node.lineno} imports {alias.name}")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id in forbidden:
                offenders.append(f"{path.name}:{node.lineno} reads {node.value.id}")

    assert not offenders, "these read config rather than being handed it: " + ", ".join(offenders)
