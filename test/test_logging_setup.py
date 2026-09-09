# test/test_logging_setup.py
"""Emitting log lines the collector can actually read.

Nothing in this repository consumes these lines. service-orchestrator tees
every child's stdout to `project/logging/<service>`, and logging-service parses
what arrives. So the contract being tested here lives in another repository,
and the only way to keep it honest is to check the output against that parser's
own expectations.
"""
import logging
import re
import subprocess
import sys
from pathlib import Path


from dependencies import logging_setup

#: Copied verbatim from logging-service's app/dependencies/log_line.py.
#: A line that does not match this is recorded by the collector as INFO
#: whatever severity it claims -- so every error would be filed as routine.
COLLECTOR_STAMPED = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*\s*-\s*"
    r"(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\s*-\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


# Root logger state is restored by an autouse fixture in conftest.py.

def _emit(level="INFO", message="disk is filling up", call="warning"):
    """Configure logging and capture one line from a real subprocess.

    In-process capture would not prove the line ever reaches a pipe, which is
    the only thing the orchestrator can tee.
    """
    code = (
        "import sys; sys.path[:0] = ['app']\n"
        "from dependencies import logging_setup\n"
        f"logging_setup.configure({level!r})\n"
        "import logging\n"
        f"logging.{call}({message!r})\n"
    )
    done = subprocess.run([sys.executable, "-c", code],
                          cwd=Path(__file__).resolve().parent.parent,
                          capture_output=True, text=True)
    return (done.stdout + done.stderr).strip()


def test_info_actually_emits():
    """The root logger defaults to WARNING, so an unconfigured service drops
    every info() call silently. Seven services inherited that: they appeared to
    log, emitted nothing, and the tee forwarded nothing."""
    assert "service started" in _emit(message="service started", call="info")


def test_the_line_matches_what_the_collector_parses():
    """logging-service reads the severity off the front of the line. Its own
    comment says every Bytronic service uses this format -- which was an
    assumption, not a fact, until this test."""
    line = _emit(message="disk is filling up", call="warning")
    matched = COLLECTOR_STAMPED.match(line)
    assert matched, f"the collector cannot parse this line: {line!r}"
    assert matched.group(1).upper() == "WARNING"
    assert matched.group(2) == "disk is filling up"


def test_severity_is_carried_not_flattened():
    """A line the collector cannot read is filed as INFO whatever it says, so
    an error would be recorded as routine and nothing would look wrong."""
    for call, expected in (("error", "ERROR"), ("info", "INFO")):
        line = _emit(level="DEBUG", message="x", call=call)
        matched = COLLECTOR_STAMPED.match(line)
        assert matched and matched.group(1).upper() == expected, line


def test_debug_is_withheld_at_the_default_level():
    assert _emit(level="INFO", message="chatty", call="debug") == ""


def test_debug_is_emitted_when_asked_for():
    assert "chatty" in _emit(level="DEBUG", message="chatty", call="debug")


def test_an_unknown_level_falls_back_rather_than_crashing():
    """A typo in a customer's config must not stop the service starting."""
    logging_setup.configure("VERBOSE")
    assert logging.getLogger().level == logging.INFO


def test_configuring_twice_does_not_duplicate_every_line():
    """basicConfig is a no-op once handlers exist, but a hand-rolled setup that
    appends would double every line -- and the collector would record each one
    twice."""
    logging_setup.configure("INFO")
    logging_setup.configure("INFO")
    assert len(logging.getLogger().handlers) == 1


def test_no_runtime_code_reports_through_print():
    """print() is invisible under the orchestrator.

    A service's stdout is a pipe, so Python block-buffers print() output. It
    appears only once several KB accumulate, and is lost outright if the
    service crashes -- exactly when it is wanted. PYTHONUNBUFFERED does not
    help: PyInstaller's bootloader configures the interpreter itself and does
    not honour it. Measured on a packaged binary: 0 lines after one message,
    301 after three hundred.

    Logging handlers flush per record, so anything a service needs seen must
    go through logging. This is checked by reading the source because the
    alternative -- running a real binary against a real broker -- lives in
    docker-local and is not part of this suite.
    """
    import ast
    from pathlib import Path

    # Parsed rather than grepped: the word print() appears in docstrings and
    # comments explaining precisely why not to use it, and a text search flags
    # those instead of real calls.
    app = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in sorted(app.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                offenders.append(f"{path.relative_to(app.parent)}:{node.lineno}")
    assert not offenders, (
        "these report through print(), which the orchestrator cannot see: "
        + ", ".join(offenders))
