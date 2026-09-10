from pathlib import Path

import pytest
import yaml

from dependencies import loadConfig

ROOT = Path(__file__).resolve().parent.parent


def test_parse_cli_requires_config():
    with pytest.raises(SystemExit):
        loadConfig.parse_cli([])


def test_get_config_loads_named_file(tmp_path, monkeypatch):
    monkeypatch.setattr(loadConfig, "_ACTIVE", None)
    path = tmp_path / "cfg.yaml"
    path.write_text("mqtt:\n  mqtt_ip: 10.0.0.1\nservice: {}\n")
    config = loadConfig.get_config(str(path))
    assert config["mqtt"]["mqtt_ip"] == "10.0.0.1"


def test_shipped_config_yaml_is_valid_mapping():
    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "mqtt" in data and "service" in data and "logging" in data
    assert "capture_type" in data["service"]["trigger"]


def test_example_config_matches_required_shape():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert data["service"]["trigger"]["trigger_type"] in ("software", "hardware")
    assert data["service"]["trigger"]["capture_type"] in ("single", "continuous")
    assert "archiving" in data["service"]
