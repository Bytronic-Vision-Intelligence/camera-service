"""Reusable external / hardware trigger helpers for camera backends.

Vendor-specific setup lives in ``spinnaker_trigger``.
Shared pieces here: config loading, edge detection, and the GPIO poll loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from queue import Queue
from threading import Event
from typing import Callable, Optional

import numpy as np


logger = logging.getLogger(__name__)


class CameraLossError(RuntimeError):
    """Camera device link lost (e.g. Spinnaker -1010)."""


def is_camera_loss_error(exc: BaseException) -> bool:
    """True when the exception indicates a dropped camera link."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        msg = str(current).lower()
        if (
            "-1010" in msg
            or "try reconnecting the device" in msg
            or "error reading from device" in msg
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def report_camera_loss(exc: BaseException, *, queue: Optional[Queue] = None) -> CameraLossError:
    """Log a single CAMERA LOSS report and optionally enqueue it for main."""
    loss = CameraLossError(str(exc))
    # critical(), and nothing else. This used to print the same line as well,
    # for a terminal nobody watches -- and print() is the one place it could
    # not arrive, because stdout under the orchestrator is a pipe that
    # block-buffers until several KB accumulate and loses the lot if the
    # service dies. A camera loss is exactly when the service dies.
    logger.critical("CAMERA LOSS: %s", exc)
    if queue is not None:
        try:
            queue.put(loss)
        except Exception:
            logger.debug("Failed enqueueing CameraLossError", exc_info=True)
    return loss


@dataclass(frozen=True)
class HardwareTriggerConfig:
    """Settings for an external light-gate / GPIO trigger."""

    enabled: bool = False
    source: str = "Line0"
    activation: str = "RisingEdge"  # RisingEdge | FallingEdge | AnyEdge
    poll_interval_s: float = 0.001

    @classmethod
    def from_trigger_settings(cls, cfg: dict) -> "HardwareTriggerConfig":
        """Build from the `service.trigger` section.

        Args:
            cfg: the trigger settings. Required: this used to fall back to
                reading the process-global config, which meant the caller could
                not tell whether it had configured the trigger or not.
        """
        trigger_type = str(cfg.get("trigger_type", "")).lower()
        return cls(
            enabled=trigger_type == "external",
            source=str(cfg.get("trigger_source", "Line0")),
            activation=str(cfg.get("trigger_activation", "RisingEdge")),
            poll_interval_s=float(cfg.get("trigger_poll_interval_s", 0.001)),
        )


def edge_detected(previous: bool, current: bool, activation: str) -> bool:
    """Return True when ``previous -> current`` matches the configured edge."""
    if activation == "RisingEdge":
        return (not previous) and current
    if activation == "FallingEdge":
        return previous and (not current)
    if activation == "AnyEdge":
        return previous != current
    logger.warning("Unknown trigger_activation %s; using RisingEdge", activation)
    return (not previous) and current


def wait_for_gpio_edge_frames(
    *,
    read_line: Callable[[], bool],
    capture_frame: Callable[[], np.ndarray],
    queue: Queue,
    stop_event: Event,
    config: HardwareTriggerConfig,
    initial_status: Optional[bool] = None,
) -> None:
    """Poll a digital input and enqueue one captured frame per matching edge.

    Used by cameras that cannot arm native GenICam ``TriggerMode`` (e.g. FLIR AX5)
    and by any backend that prefers software edge detect on a GPIO line.
    """
    previous = initial_status
    if previous is None:
        try:
            previous = read_line()
        except Exception as e:
            logger.error("Initial line read failed: %s", e, exc_info=True)
            previous = False

    logger.info(
        "Waiting for %s on %s (idle=%s)",
        config.activation,
        config.source,
        previous,
    )

    while not stop_event.is_set():
        try:
            current = read_line()
        except Exception as e:
            if stop_event.is_set():
                break
            if is_camera_loss_error(e):
                report_camera_loss(e, queue=queue)
                break
            logger.error("Line status read failed: %s", e, exc_info=True)
            time.sleep(0.05)
            continue

        if edge_detected(previous, current, config.activation):
            logger.info(
                "GPIO edge detected (%s -> %s); capturing frame",
                previous,
                current,
            )
            try:
                queue.put(capture_frame())
            except Exception as e:
                if stop_event.is_set():
                    break
                if is_camera_loss_error(e):
                    report_camera_loss(e, queue=queue)
                    break
                logger.error("Failed to capture after GPIO edge: %s", e, exc_info=True)

        previous = current
        time.sleep(config.poll_interval_s)
