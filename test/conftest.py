import logging

import pytest


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
