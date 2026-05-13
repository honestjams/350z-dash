"""
Auto-detect a USB-to-serial adapter for the Consult-II cable.

Preference order (most to least likely to be a working KKL/Consult cable):
    1. FTDI FT232R       (best — most KKL cables and the ECUtalk cable)
    2. Prolific PL2303   (older but works)
    3. CH340 / CH341     (cheap clones, sometimes problematic)
    4. Any other USB serial
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from serial.tools import list_ports

logger = logging.getLogger(__name__)


# (vid, pid, friendly_name, priority) — lower priority = preferred.
KNOWN_CHIPS = [
    (0x0403, 0x6001, "FTDI FT232R", 1),
    (0x0403, 0x6010, "FTDI FT2232", 1),
    (0x0403, 0x6014, "FTDI FT232H", 1),
    (0x0403, 0x6015, "FTDI FT231X", 1),
    (0x067B, 0x2303, "Prolific PL2303", 2),
    (0x10C4, 0xEA60, "Silicon Labs CP210x", 2),
    (0x1A86, 0x7523, "CH340", 3),
    (0x1A86, 0x5523, "CH341", 3),
]


@dataclass(frozen=True)
class DetectedPort:
    device: str          # /dev/ttyUSB0, COM3, etc.
    chip_name: str
    priority: int        # 1 = best
    vid: int | None
    pid: int | None


def _classify(port) -> DetectedPort:
    vid, pid = port.vid, port.pid
    for known_vid, known_pid, name, prio in KNOWN_CHIPS:
        if vid == known_vid and pid == known_pid:
            return DetectedPort(port.device, name, prio, vid, pid)

    # Unknown chip — best effort by description.
    name = port.description or "unknown USB serial"
    return DetectedPort(port.device, name, 9, vid, pid)


def iter_serial_ports() -> Iterator[DetectedPort]:
    """Yield all candidate serial ports, sorted best-first."""
    candidates = []
    for port in list_ports.comports():
        # Skip Bluetooth ports on macOS, virtual ports on Linux.
        if port.device.startswith("/dev/cu.Bluetooth"):
            continue
        if port.device.startswith("/dev/ttyS") and not port.vid:
            # Onboard UARTs (no USB vid/pid) — only the Pi's internal,
            # not a USB cable.
            continue
        candidates.append(_classify(port))

    candidates.sort(key=lambda p: (p.priority, p.device))
    yield from candidates


def find_best_port(override: str | None = None) -> DetectedPort | None:
    """
    Pick the best candidate port. If `override` is given, use that
    specific device regardless of detection.
    """
    if override:
        # User-specified — still try to identify it for logging.
        for port in list_ports.comports():
            if port.device == override:
                return _classify(port)
        logger.warning("Config specifies %s but it's not currently visible. "
                       "Will try to open it anyway.", override)
        return DetectedPort(override, "user-specified", 0, None, None)

    candidates = list(iter_serial_ports())
    if not candidates:
        logger.info("No USB serial adapters found.")
        return None

    if len(candidates) > 1:
        logger.info("Multiple serial adapters present:")
        for c in candidates:
            logger.info("  %s — %s (priority %d)", c.device, c.chip_name, c.priority)

    best = candidates[0]
    logger.info("Selected %s — %s", best.device, best.chip_name)
    return best
