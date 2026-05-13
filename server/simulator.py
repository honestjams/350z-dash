"""
Server-side telemetry simulator.

Generates realistic-looking data on a 30-second acceleration → cruise →
brake loop. The UI doesn't know (or care) whether the data is from this
simulator or a real ECU — both push the same JSON shape over the same
WebSocket, so the dashboard behaves identically.

Use this for laptop dev, demos, and anytime you want to test the UI
without starting the car.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from .consult import TelemetrySnapshot

logger = logging.getLogger(__name__)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class Simulator:
    """
    Mirrors the browser demo loop. 30s cycle:
      0–8s   : hard acceleration through gears (0 → 7000 rpm, 0 → 200 km/h)
      8–12s  : shift-up flatten (drop to 3500 rpm, hold 180 km/h)
      12–20s : cruise with minor variations
      20–25s : brake to 40 km/h
      25–30s : low-speed corner (60 km/h, ~2200 rpm)
    """

    def __init__(self, poll_interval: float = 0.05):
        self.poll_interval = poll_interval
        self.snapshot = TelemetrySnapshot()
        self._start = time.monotonic()
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return True

    async def connect(self) -> bool:
        logger.info("Simulator running (no ECU required).")
        return True

    async def stream(self):
        while not self._stop.is_set():
            self._step()
            self.snapshot.frames_received += 1
            self.snapshot.last_update_ts = time.monotonic()
            yield self.snapshot
            await asyncio.sleep(self.poll_interval)

    def _step(self):
        s = self.snapshot
        t = time.monotonic() - self._start
        cycle = t % 30.0

        if cycle < 8.0:
            # Acceleration
            target_rpm = 1500 + (cycle / 8.0) * 5500
            target_speed = 30 + cycle * 22
            target_tps = 90 + math.sin(t * 7) * 4
        elif cycle < 12.0:
            # Shift-up flatten
            target_rpm = 3500
            target_speed = 180
            target_tps = 60
        elif cycle < 20.0:
            # Cruise
            target_rpm = 3200 + math.sin(t * 2) * 400
            target_speed = 160 + math.sin(t * 0.5) * 20
            target_tps = 35 + math.sin(t * 3) * 15
        elif cycle < 25.0:
            # Brake
            target_rpm = 1800
            target_speed = 40
            target_tps = 0
        else:
            # Low-speed corner
            target_rpm = 2200
            target_speed = 60
            target_tps = 25 + math.sin(t * 4) * 10

        s.rpm = _lerp(s.rpm, target_rpm, 0.08)
        s.speed = _lerp(s.speed, target_speed, 0.06)
        s.tps = _lerp(s.tps, target_tps, 0.15)

        # Derived / slower-moving values
        s.coolant = _lerp(s.coolant, 85 + (s.tps / 100) * 6 + math.sin(t * 0.05) * 2, 0.005)
        s.oilTemp = _lerp(s.oilTemp, 90 + (s.tps / 100) * 25 + (s.rpm / 8000) * 10, 0.008)
        s.oilPressure = _lerp(s.oilPressure, 0.8 + (s.rpm / 8000) * 5.5, 0.08)
        s.map = _lerp(s.map, -0.6 + (s.tps / 100) * 0.6, 0.08)
        s.afr = _lerp(s.afr, 14.7 - (s.tps / 100) * 2.8, 0.15)
        s.battery = _lerp(s.battery, 14.2 - (s.tps / 100) * 0.4, 0.03)
        s.ignAdv = _lerp(s.ignAdv, 35 - (s.tps / 100) * 18 - (s.rpm / 8000) * 4, 0.15)
        s.maf = _lerp(s.maf, 0.6 + (s.rpm / 8000) * 3.5 + (s.tps / 100) * 0.4, 0.15)
        s.injPW = _lerp(s.injPW, 1.5 + (s.tps / 100) * 8 + (s.rpm / 8000) * 2, 0.15)

    async def close(self):
        self._stop.set()
        logger.info("Simulator stopped.")
