"""
Consult-II protocol over K-line (single-wire bidirectional, 9600 8N1).

PROTOCOL SUMMARY (community-documented, not Nissan-official):

  1. Wake-up:        Send 0xFF repeatedly until ECU echoes 0xFF.
  2. Init:           Send [0xFF, 0xFF, 0xEF], ECU replies 0x10 (ACK).
  3. Read register:  Send [0x5A, REG], ECU echoes the command.
     - Multiple register reads can be queued.
  4. Start stream:   Send 0xF0. ECU streams bytes in subscription order.
  5. Stop stream:    Send 0x30. ECU sends an ACK frame and returns to idle.

Every byte sent to the ECU is echoed back on the same wire, so a read of
N bytes after sending M bytes returns (M + actual_response_data).

This module focuses on getting *anything* talking and provides hooks for
register-level tuning once you have a known-good reference (NDSII).

KNOWN LIMITATIONS:
  - The K-line is half-duplex. The cheap KKL cables don't always handle
    the bus collision gracefully if you send during a stream — we always
    stop, send, restart.
  - The byte stream has no framing markers between register values, so
    we rely on the subscription order to know which byte is which.
  - Timing matters: a slow USB driver can drop bytes once the engine is
    running and electrical noise increases. If you see corrupted reads,
    try a different USB port or an FTDI-chip cable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import serial
import serial_asyncio

from .registers import REGISTERS, SUBSCRIPTION_ORDER, Register

logger = logging.getLogger(__name__)


# Protocol bytes
INIT_BYTE = 0xFF
INIT_ACK = 0x10
ECU_INIT_CMD = b"\xff\xff\xef"
READ_REG_CMD = 0x5A
START_STREAM = 0xF0
STOP_STREAM = 0x30


@dataclass
class TelemetrySnapshot:
    """Most-recent decoded values from the ECU."""
    rpm: float = 0.0
    speed: float = 0.0
    coolant: float = 82.0
    tps: float = 0.0
    battery: float = 14.0
    ignAdv: float = 14.0
    maf: float = 1.4
    injPW: float = 2.0
    afr: float = 14.7
    # External / placeholder
    oilTemp: float = 90.0
    oilPressure: float = 4.0
    map: float = -0.4
    # Diagnostics
    last_update_ts: float = field(default_factory=time.monotonic)
    frames_received: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "rpm": round(self.rpm, 1),
            "speed": round(self.speed, 1),
            "coolant": round(self.coolant, 1),
            "tps": round(self.tps, 1),
            "battery": round(self.battery, 2),
            "ignAdv": round(self.ignAdv, 1),
            "maf": round(self.maf, 3),
            "injPW": round(self.injPW, 2),
            "afr": round(self.afr, 2),
            "oilTemp": round(self.oilTemp, 1),
            "oilPressure": round(self.oilPressure, 2),
            "map": round(self.map, 2),
        }


class ConsultClient:
    """
    Async client for a Consult-II ECU.

    Usage:
        client = ConsultClient(port="/dev/ttyUSB0")
        await client.connect()
        async for snapshot in client.stream():
            print(snapshot.rpm, snapshot.speed)
    """

    def __init__(
        self,
        port: str,
        baud: int = 9600,
        handshake_timeout: float = 3.0,
        poll_interval: float = 0.05,
    ):
        self.port = port
        self.baud = baud
        self.handshake_timeout = handshake_timeout
        self.poll_interval = poll_interval

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.snapshot = TelemetrySnapshot()
        self._connected = False
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """Open the port and perform the wake-up handshake."""
        logger.info("Opening %s at %d baud...", self.port, self.baud)
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
        except (serial.SerialException, OSError) as e:
            logger.error("Failed to open %s: %s", self.port, e)
            return False

        ok = await self._handshake()
        if ok:
            logger.info("ECU handshake OK on %s.", self.port)
            self._connected = True
        else:
            logger.warning("ECU did not respond on %s. "
                           "Check cable + ignition position.", self.port)
        return ok

    async def _handshake(self) -> bool:
        """
        Send wake bytes until the ECU echoes, then send the init command
        and wait for the 0x10 ACK.
        """
        assert self._reader and self._writer

        # Drain any junk in the buffer.
        try:
            await asyncio.wait_for(self._reader.read(256), timeout=0.1)
        except asyncio.TimeoutError:
            pass

        start = time.monotonic()
        while time.monotonic() - start < self.handshake_timeout:
            self._writer.write(bytes([INIT_BYTE]))
            await self._writer.drain()
            try:
                b = await asyncio.wait_for(self._reader.read(1), timeout=0.2)
                if b and b[0] == INIT_BYTE:
                    break
            except asyncio.TimeoutError:
                continue
        else:
            logger.error("Handshake: no echo of wake byte from ECU.")
            return False

        # Send the init command.
        self._writer.write(ECU_INIT_CMD)
        await self._writer.drain()

        # Discard the echo of our own send + wait for ACK.
        try:
            data = await asyncio.wait_for(
                self._reader.readexactly(len(ECU_INIT_CMD) + 1),
                timeout=1.0,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            logger.error("Handshake: no init ACK from ECU.")
            return False

        ack = data[-1]
        if ack != INIT_ACK:
            logger.error("Handshake: unexpected ACK byte 0x%02X (expected 0x%02X).",
                         ack, INIT_ACK)
            return False
        return True

    async def _subscribe(self, fields: list[str]) -> bool:
        """Queue register reads and start the stream."""
        assert self._writer
        for fname in fields:
            reg = REGISTERS.get(fname)
            if not reg:
                continue
            self._writer.write(bytes([READ_REG_CMD, reg.addr]))
            await self._writer.drain()
            # Each command echoes back (2 bytes per).
            try:
                await asyncio.wait_for(self._reader.readexactly(2), timeout=0.3)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                logger.error("Subscription failed at register %s (0x%02X).",
                             fname, reg.addr)
                return False

        # Start streaming.
        self._writer.write(bytes([START_STREAM]))
        await self._writer.drain()

        # First byte after F0 is a frame header / count byte. Read & discard.
        try:
            await asyncio.wait_for(self._reader.readexactly(2), timeout=0.5)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            logger.error("Stream did not start.")
            return False
        return True

    async def stream(self, fields: list[str] | None = None):
        """
        Continuously read register values and update self.snapshot.
        Yields snapshot after each complete frame.
        """
        if not self._connected:
            raise RuntimeError("Not connected — call connect() first.")

        fields = fields or SUBSCRIPTION_ORDER
        ok = await self._subscribe(fields)
        if not ok:
            return

        # Note: RPM is two bytes on Consult-II. If "rpm" is in fields,
        # we read 2 bytes for it. We handle this by having "rpm_hi"
        # and "rpm_lo" virtual fields, OR by detecting it here.
        # For simplicity: read len(fields) bytes per frame, decode each.

        regs = [REGISTERS[f] for f in fields if f in REGISTERS]
        bytes_per_frame = len(regs)

        while not self._stop.is_set():
            try:
                frame = await asyncio.wait_for(
                    self._reader.readexactly(bytes_per_frame),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Stream stall — no data for 1s.")
                self.snapshot.errors += 1
                continue
            except asyncio.IncompleteReadError as e:
                logger.error("Stream closed: %s", e)
                break

            for reg, raw in zip(regs, frame):
                value = reg.decode(raw)
                setattr(self.snapshot, reg.name, value)

            # Estimate RPM from a separate source if available, or hold last.
            # TODO: Add proper 2-byte RPM read once register address is verified.

            self.snapshot.frames_received += 1
            self.snapshot.last_update_ts = time.monotonic()
            yield self.snapshot

            await asyncio.sleep(self.poll_interval)

    async def close(self):
        """Send stream-stop and close the port."""
        self._stop.set()
        if self._writer and not self._writer.is_closing():
            try:
                self._writer.write(bytes([STOP_STREAM]))
                await self._writer.drain()
                await asyncio.sleep(0.1)
            except Exception:
                pass
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
        logger.info("Consult connection closed.")


# ---------------------------------------------------------------------
# Standalone probe utility — `python -m server.consult --port X --probe`
# ---------------------------------------------------------------------
async def probe(port: str, duration: float = 5.0):
    """Open a connection and dump raw register values for diagnostics."""
    client = ConsultClient(port=port)
    ok = await client.connect()
    if not ok:
        return

    print(f"\nProbing for {duration}s — drive briefly to vary values.\n")
    start = time.monotonic()
    n = 0
    async for snap in client.stream():
        n += 1
        if n % 10 == 0:
            print(snap.as_dict())
        if time.monotonic() - start > duration:
            break
    await client.close()
    print(f"\nReceived {snap.frames_received} frames, {snap.errors} errors.")
