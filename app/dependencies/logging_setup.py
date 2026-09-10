"""Configure a service's logging so the orchestrator can forward it.

Services do not write log files. They print, service-orchestrator tees every
child's stdout to ``project/logging/<service>``, and logging-service is the
only thing that writes to disk. That means the format here is not a local
style choice -- it is the wire format of a pipeline in another repository.

logging-service reads the severity off the front of each line. A line it
cannot parse is recorded as INFO whatever it claims, so an unparseable error
is filed as routine and nothing anywhere looks wrong. Its parser expects
``%(asctime)s - %(levelname)s - %(message)s``, and its own comment says every
Bytronic service uses that format -- which was an assumption until this module
made it true.

Two things this fixes that were silently broken:

  * The root logger defaults to WARNING, so an unconfigured service dropped
    every ``info()`` call. Services appeared to log and emitted nothing.
  * Handlers flush per record, unlike ``print()``, whose output sits in a block
    buffer when stdout is a pipe -- which it always is under the orchestrator.
    Buffered lines arrive late and are lost outright if the service crashes,
    which is exactly when they matter.
"""

import logging
import sys

#: What logging-service's log_line.py parses. Changing it silently downgrades
#: every line to INFO at the far end. Pinned by
#: test/test_logging_setup.py::test_the_line_matches_what_the_collector_parses
LINE_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

DEFAULT_LEVEL = "INFO"


def configure(level: str = DEFAULT_LEVEL) -> None:
    """Send log records to stdout in the format the collector reads.

    Args:
        level: a level name from the service's config. Anything unrecognised
            falls back to INFO rather than raising -- a typo in a customer's
            config must not stop the service starting.
    """
    resolved = getattr(logging, str(level or "").strip().upper(), None)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    root = logging.getLogger()
    # Replaced, not appended. Calling this twice must not double every line,
    # which at the far end would record each event twice.
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LINE_FORMAT))
    root.addHandler(handler)
    root.setLevel(resolved)
