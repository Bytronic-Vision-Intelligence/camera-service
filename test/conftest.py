import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHAPE_CONFIG = ROOT / "config.example.yaml"


@pytest.fixture(autouse=True)
def use_local_config(monkeypatch):
    """Pin sys.argv so accidental loadConfig.parse_cli() calls see a valid --config.

    loadConfig resolves from sys.argv when callers omit argv. Under pytest that
    would otherwise be pytest's own flags. Individual tests that call
    main([...]) or monkeypatch get_config override this themselves.
    """
    monkeypatch.setattr(
        "sys.argv",
        ["pytest", "--config", str(SHAPE_CONFIG)],
    )


@pytest.fixture(autouse=True)
def _forget_the_resolved_config():
    """Clear the config path this process resolved.

    loadConfig keeps it in a module global so that later reads cannot drift
    onto a different file. Left set between tests, a test that never named a
    config silently reads whichever one the previous test did.
    """
    from dependencies import loadConfig
    loadConfig._ACTIVE = None
    yield
    loadConfig._ACTIVE = None


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Put the root logger back after every test.

    logging_setup.configure() replaces the root handlers deliberately -- a
    service calling it twice must not double every line. But pytest's own
    capture handler lives there too, so a test that configures logging silently
    disables caplog for every test that runs after it, in any file. The symptom
    is an empty caplog.text somewhere unrelated.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
