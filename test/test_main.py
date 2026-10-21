from queue import Queue

import numpy as np
import pytest

from fakes import FakeMQTTClient, FakeMQTTConfig, FakeThread

import main
def test_main_answers_help_before_looking_for_a_config(monkeypatch):
    monkeypatch.setattr(
        main.loadConfig,
        "get_config",
        lambda supplied=None: pytest.fail("looked for a config before --help"),
    )
    with pytest.raises(SystemExit) as exit_info:
        main.main(["--help"])
    assert exit_info.value.code == 0

def test_main_refuses_an_empty_config_path(monkeypatch):
    monkeypatch.setattr(main.loadConfig, "_ACTIVE", None)
    with pytest.raises(SystemExit, match="--config is required"):
        main.main(["--config", ""])