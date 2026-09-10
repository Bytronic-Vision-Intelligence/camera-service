# test/test_load_config.py
"""Reading the configuration a service was told to run with.

--config is required and there is no fallback. That is the behaviour under
test: a service that found a config on its own would start whenever one
happened to be beside it -- a stale copy, the example shipped in the package,
or another instance's file in a shared directory -- and would be the wrong
service while looking entirely healthy.
"""
import re

import pytest

from dependencies import loadConfig


@pytest.fixture(autouse=True)
def _forget_the_active_config(monkeypatch):
    """Reset the resolved path between tests.

    It is deliberately process state -- a service reads one config for its
    lifetime -- so without this a test inherits the previous one's file.
    """
    monkeypatch.setattr(loadConfig, "_ACTIVE", None)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mqtt:\n  mqtt_ip: 10.0.0.1\n")
    return path


def test_the_named_config_is_read(config_file):
    assert loadConfig.get_config(config_file)["mqtt"]["mqtt_ip"] == "10.0.0.1"


def test_a_second_instance_reads_its_own_file(tmp_path, config_file):
    """The reason the flag exists: one binary, several instances."""
    other = tmp_path / "instance-2.yaml"
    other.write_text("mqtt:\n  mqtt_ip: 10.0.0.2\n")
    assert loadConfig.get_config(other)["mqtt"]["mqtt_ip"] == "10.0.0.2"


@pytest.mark.parametrize("nothing", ["", "   ", "\t", "\n", None])
def test_naming_no_config_is_refused(nothing):
    """An empty value reaches a service from an unset shell variable or a
    launcher that dropped an argument."""
    with pytest.raises(SystemExit, match="--config is required"):
        loadConfig.resolve_config_path(nothing)


def test_nothing_is_read_from_beside_the_binary(tmp_path, monkeypatch):
    """The whole point. Even with a perfectly good config sitting next to the
    service, an unnamed one must not be found: that file could be a stale
    deployment, the packaged example, or another instance's."""
    beside = tmp_path / "config.yaml"
    beside.write_text("mqtt:\n  mqtt_ip: 10.9.9.9\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="--config is required"):
        loadConfig.get_config("")


def test_a_named_file_that_is_absent_is_refused(tmp_path):
    missing = tmp_path / "not-here.yaml"
    with pytest.raises(SystemExit, match=re.escape(str(missing))):
        loadConfig.get_config(missing)


@pytest.mark.parametrize("text", ["", "\n", "- a\n- b\n", "just a string\n"])
def test_an_empty_or_non_mapping_config_reads_as_empty(tmp_path, text):
    """A list or a bare string is not a config section. Returning it would push
    an AttributeError into whatever asked for a key."""
    path = tmp_path / "c.yaml"
    path.write_text(text)
    assert loadConfig.get_config(path) == {}


def test_later_reads_use_the_file_the_service_started_with(tmp_path):
    """A service told to use instance-2.yaml must never drift onto another
    file, and its errors must name the one it actually opened."""
    other = tmp_path / "instance-2.yaml"
    other.write_text("mqtt: {}\n")
    loadConfig.get_config(other)
    assert loadConfig.config_path() == other
    with pytest.raises(KeyError, match="instance-2.yaml"):
        loadConfig.return_config_value("absent")


def test_asking_for_the_path_before_parsing_is_an_error():
    """Reaching for config before arguments are parsed is a wiring mistake,
    and answering with a guess would hide it."""
    with pytest.raises(SystemExit, match="no configuration"):
        loadConfig.config_path()


def test_the_config_flag_is_required():
    """A bare run exits non-zero rather than guessing which instance it is."""
    with pytest.raises(SystemExit) as exit_info:
        loadConfig.parse_cli([])
    assert exit_info.value.code != 0


def test_help_still_exits_zero(capsys):
    """The release build smoke-tests every binary with --help and fails on a
    non-zero exit. argparse handles --help before it enforces required
    arguments, so a required --config does not break the build."""
    with pytest.raises(SystemExit) as exit_info:
        loadConfig.parse_cli(["--help"])
    assert exit_info.value.code == 0
    assert "--config" in capsys.readouterr().out


def test_the_flag_carries_the_path():
    assert loadConfig.parse_cli(["--config", "/some/where.yaml"]).config == \
        "/some/where.yaml"
