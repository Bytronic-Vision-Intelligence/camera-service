"""Spinnaker / PySpin hardware-trigger configuration."""

from __future__ import annotations

import logging
from typing import Optional

import PySpin

from dependencies.CameraLibrary.hardware_trigger import HardwareTriggerConfig

logger = logging.getLogger(__name__)


def set_enum(nodemap, node_name: str, entry_name: str) -> bool:
    node = PySpin.CEnumerationPtr(nodemap.GetNode(node_name))
    if not PySpin.IsAvailable(node):
        logger.warning("Node %s is unavailable", node_name)
        return False
    if not PySpin.IsWritable(node):
        logger.warning("Node %s is not writable", node_name)
        return False
    entry = PySpin.CEnumEntryPtr(node.GetEntryByName(entry_name))
    if not PySpin.IsAvailable(entry) or not PySpin.IsReadable(entry):
        available = []
        try:
            for e in node.GetEntries():
                ep = PySpin.CEnumEntryPtr(e)
                if PySpin.IsAvailable(ep) and PySpin.IsReadable(ep):
                    available.append(ep.GetSymbolic())
        except Exception:
            logger.debug(
                "Failed to enumerate available entries for node %s",
                node_name,
                exc_info=True,
            )
        logger.warning(
            "Enum entry %s.%s is not available (have: %s)",
            node_name,
            entry_name,
            available or "?",
        )
        return False
    node.SetIntValue(entry.GetValue())
    return True


class SpinnakerHardwareTrigger:
    """Configure and operate external trigger on a Spinnaker camera.

    Modes:
      - ``native``: GenICam TriggerMode / TriggerSource (Blackfly etc.)
      - ``gpio_poll``: poll LineStatus and capture on edge (FLIR AX5 etc.)
      - ``off``: free-run / software path
    """

    def __init__(self, cam, config: HardwareTriggerConfig):
        self._cam = cam
        self.config = config
        self.mode = "off"  # off | native | gpio_poll
        self._last_line_status: Optional[bool] = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def uses_gpio_poll(self) -> bool:
        return self.mode == "gpio_poll"

    def configure(self, nodemap=None) -> None:
        if nodemap is None:
            nodemap = self._cam.GetNodeMap()

        if not self.config.enabled:
            self.mode = "off"
            set_enum(nodemap, "TriggerMode", "Off")
            return

        self._ensure_not_acquiring()

        trigger_mode = nodemap.GetNode("TriggerMode")
        if PySpin.IsAvailable(trigger_mode) and PySpin.IsWritable(trigger_mode):
            self._configure_native(nodemap)
        else:
            logger.info(
                "TriggerMode unavailable — using GPI LineStatus edge detect"
            )
            self._configure_gpio_poll(nodemap)

    def reset(self, nodemap=None) -> None:
        if self.mode != "native":
            self.mode = "off"
            return
        if nodemap is None and self._cam is not None:
            try:
                nodemap = self._cam.GetNodeMap()
            except Exception:
                nodemap = None
        if nodemap is not None:
            set_enum(nodemap, "TriggerMode", "Off")
        self.mode = "off"

    def read_line(self) -> bool:
        """Read GPI status for the configured line (gpio_poll mode)."""
        nodemap = self._cam.GetNodeMap()
        status = PySpin.CBooleanPtr(nodemap.GetNode("LineStatus"))
        if not PySpin.IsAvailable(status) or not PySpin.IsReadable(status):
            self._select_line(nodemap, self.config.source)
            status = PySpin.CBooleanPtr(nodemap.GetNode("LineStatus"))
            if not PySpin.IsAvailable(status) or not PySpin.IsReadable(status):
                raise RuntimeError("LineStatus is not readable on this camera")
        return bool(status.GetValue())

    def sample_idle_line(self) -> Optional[bool]:
        try:
            self._last_line_status = self.read_line()
            logger.info("GPIO idle LineStatus=%s", self._last_line_status)
            return self._last_line_status
        except Exception:
            logger.debug("Could not read initial LineStatus", exc_info=True)
            return self._last_line_status

    @property
    def last_line_status(self) -> Optional[bool]:
        return self._last_line_status

    def _ensure_not_acquiring(self) -> None:
        if self._cam is None:
            return
        try:
            if self._cam.IsStreaming():
                logger.warning("Camera was still streaming; calling EndAcquisition()")
                self._cam.EndAcquisition()
        except Exception:
            try:
                self._cam.EndAcquisition()
            except Exception:
                logger.debug("EndAcquisition failed", exc_info=True)

    def _prefer_newest_buffer(self) -> None:
        try:
            stream_nodemap = self._cam.GetTLStreamNodeMap()
            set_enum(stream_nodemap, "StreamBufferHandlingMode", "NewestOnly")
        except Exception:
            logger.debug("Could not set StreamBufferHandlingMode", exc_info=True)

    def _select_line(self, nodemap, source: str) -> None:
        if not set_enum(nodemap, "LineSelector", source):
            raise RuntimeError(f"Failed to select GPIO line {source}")
        set_enum(nodemap, "LineMode", "Input")

    def _configure_native(self, nodemap) -> None:
        source = self.config.source
        activation = self.config.activation

        if not set_enum(nodemap, "TriggerMode", "Off"):
            set_enum(nodemap, "TriggerSelector", "FrameStart")
            if not set_enum(nodemap, "TriggerMode", "Off"):
                raise RuntimeError("TriggerMode is not writable")

        self._select_line(nodemap, source)

        if not set_enum(nodemap, "TriggerSelector", "FrameStart"):
            raise RuntimeError("Failed to set TriggerSelector to FrameStart")
        if not set_enum(nodemap, "TriggerSource", source):
            raise RuntimeError(f"Failed to set TriggerSource to {source}")
        if not set_enum(nodemap, "TriggerActivation", activation):
            raise RuntimeError(f"Failed to set TriggerActivation to {activation}")
        if not set_enum(nodemap, "TriggerMode", "On"):
            raise RuntimeError("Failed to enable TriggerMode")

        try:
            fre = PySpin.CBooleanPtr(nodemap.GetNode("AcquisitionFrameRateEnable"))
            if PySpin.IsAvailable(fre) and PySpin.IsWritable(fre):
                fre.SetValue(False)
        except Exception:
            logger.debug("AcquisitionFrameRateEnable not available", exc_info=True)

        self.mode = "native"
        logger.info(
            "Hardware trigger armed (TriggerMode): source=%s activation=%s",
            source,
            activation,
        )

    def _configure_gpio_poll(self, nodemap) -> None:
        source = self.config.source
        self._select_line(nodemap, source)
        status = PySpin.CBooleanPtr(nodemap.GetNode("LineStatus"))
        if not PySpin.IsAvailable(status) or not PySpin.IsReadable(status):
            raise RuntimeError(
                "This camera has no TriggerMode and LineStatus is unreadable; "
                "cannot use an external light-gate trigger."
            )

        self._prefer_newest_buffer()
        self.mode = "gpio_poll"
        self._last_line_status = bool(status.GetValue())
        logger.info(
            "GPIO edge trigger armed: line=%s activation=%s idle_status=%s",
            source,
            self.config.activation,
            self._last_line_status,
        )
