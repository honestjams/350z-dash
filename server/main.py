"""
Entry point for the 350Z dash server.

  python -m server.main                  # use config.yaml defaults
  python -m server.main --simulator      # force simulator
  python -m server.main --consult        # force real ECU
  python -m server.main --probe          # diagnostic register dump
  python -m server.main --port /dev/ttyUSB0  # override serial port

Serves:
  - HTTP   on http://HOST:PORT/        (the dashboard UI)
  - WebSocket on  ws://HOST:PORT/ws    (live telemetry stream)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any

import yaml
from aiohttp import WSMsgType, web

from .consult import ConsultClient, TelemetrySnapshot, probe as run_probe
from .serial_io import find_best_port
from .simulator import Simulator

logger = logging.getLogger(__name__)


ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
CONFIG_PATH = ROOT / "config.yaml"


# ---------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------
def load_config(path: Path) -> dict:
    if not path.exists():
        logger.warning("No config.yaml at %s — using built-in defaults.", path)
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def get(cfg: dict, *keys, default: Any = None) -> Any:
    """Nested get with dotted path: get(cfg, 'server', 'http_port', default=8080)."""
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


# ---------------------------------------------------------------------
# Telemetry source (Consult or Simulator) wrapped in a uniform interface
# ---------------------------------------------------------------------
class TelemetrySource:
    """Wraps either ConsultClient or Simulator with a uniform interface."""

    def __init__(self, client):
        self.client = client
        self._task: asyncio.Task | None = None
        self._latest: dict | None = None
        self._mode: str = "unknown"
        self._csv_writer = None
        self._csv_file = None

    @property
    def latest(self) -> dict | None:
        return self._latest

    @property
    def mode(self) -> str:
        return self._mode

    def set_csv_log(self, path: str | None):
        if not path:
            return
        self._csv_file = open(path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow([
            "timestamp", "rpm", "speed", "tps", "coolant",
            "oilTemp", "oilPressure", "battery", "afr",
            "ignAdv", "maf", "injPW", "map",
        ])

    async def start(self):
        self._mode = "consult" if isinstance(self.client, ConsultClient) else "simulator"
        self._task = asyncio.create_task(self._loop(), name="telemetry-loop")

    async def _loop(self):
        try:
            async for snap in self.client.stream():
                d = snap.as_dict()
                self._latest = d
                if self._csv_writer:
                    self._csv_writer.writerow([
                        snap.last_update_ts,
                        d["rpm"], d["speed"], d["tps"], d["coolant"],
                        d["oilTemp"], d["oilPressure"], d["battery"], d["afr"],
                        d["ignAdv"], d["maf"], d["injPW"], d["map"],
                    ])
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Telemetry loop crashed.")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.close()
        if self._csv_file:
            self._csv_file.close()


async def build_source(cfg: dict, args) -> TelemetrySource:
    """Decide between Consult and Simulator based on args, config, and reality."""
    if args.simulator:
        mode = "simulator"
    elif args.consult:
        mode = "consult"
    else:
        mode = get(cfg, "mode", "default", default="auto")

    port_override = args.port or get(cfg, "serial", "port")
    baud = get(cfg, "serial", "baud", default=9600)
    handshake_to = get(cfg, "serial", "handshake_timeout", default=3.0)
    poll = get(cfg, "serial", "poll_interval", default=0.05)

    if mode == "simulator":
        return TelemetrySource(Simulator(poll_interval=poll))

    # Try Consult.
    detected = find_best_port(port_override)
    if detected is None:
        if mode == "consult":
            logger.error("--consult specified but no serial port found. Exiting.")
            sys.exit(2)
        logger.info("No serial port — using simulator.")
        return TelemetrySource(Simulator(poll_interval=poll))

    consult = ConsultClient(
        port=detected.device,
        baud=baud,
        handshake_timeout=handshake_to,
        poll_interval=poll,
    )
    ok = await consult.connect()
    if not ok:
        if mode == "consult":
            logger.error("--consult specified but handshake failed. Exiting.")
            sys.exit(2)
        logger.info("ECU handshake failed — falling back to simulator.")
        return TelemetrySource(Simulator(poll_interval=poll))

    return TelemetrySource(consult)


# ---------------------------------------------------------------------
# WebSocket + HTTP server
# ---------------------------------------------------------------------
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Push telemetry to a connected dashboard at update_rate_hz."""
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)

    source: TelemetrySource = request.app["source"]
    rate = request.app["update_rate_hz"]
    interval = 1.0 / max(1, rate)
    peer = request.remote
    logger.info("WS connected: %s (mode=%s)", peer, source.mode)

    # Send a hello with the source mode so the UI can show it.
    await ws.send_json({"_meta": {"mode": source.mode}})

    try:
        last_send = 0.0
        while not ws.closed:
            now = asyncio.get_event_loop().time()
            if now - last_send >= interval and source.latest:
                await ws.send_json(source.latest)
                last_send = now

            # Drain any incoming control messages without blocking.
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=interval / 2)
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
            except asyncio.TimeoutError:
                continue
    except ConnectionResetError:
        pass
    finally:
        logger.info("WS disconnected: %s", peer)
    return ws


async def index_handler(request: web.Request) -> web.Response:
    """Serve the dashboard HTML."""
    index = UI_DIR / "index.html"
    if not index.exists():
        return web.Response(text="ui/index.html not found", status=500)
    return web.FileResponse(index)


async def health_handler(request: web.Request) -> web.Response:
    source: TelemetrySource = request.app["source"]
    return web.json_response({
        "ok": True,
        "mode": source.mode,
        "have_data": source.latest is not None,
    })


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="350Z Consult-II dashboard server")
    p.add_argument("--simulator", action="store_true", help="Force simulator mode.")
    p.add_argument("--consult", action="store_true", help="Force real ECU; fail if unavailable.")
    p.add_argument("--port", help="Serial port override (e.g. /dev/ttyUSB0).")
    p.add_argument("--probe", action="store_true",
                   help="Connect to ECU, dump raw values for 5s, then exit.")
    p.add_argument("--host", help="Bind host override.")
    p.add_argument("--http-port", type=int, help="HTTP port override.")
    return p.parse_args()


async def amain():
    args = parse_args()
    cfg = load_config(CONFIG_PATH)
    setup_logging(get(cfg, "logging", "level", default="INFO"))

    if args.probe:
        port = args.port or get(cfg, "serial", "port")
        if not port:
            detected = find_best_port(None)
            if not detected:
                logger.error("No serial port found for probe.")
                return
            port = detected.device
        await run_probe(port)
        return

    source = await build_source(cfg, args)
    source.set_csv_log(get(cfg, "logging", "csv_path"))
    await source.start()

    host = args.host or get(cfg, "server", "host", default="127.0.0.1")
    port = args.http_port or get(cfg, "server", "http_port", default=8080)
    ws_path = get(cfg, "server", "ws_path", default="/ws")
    rate = get(cfg, "server", "update_rate_hz", default=15)

    app = web.Application()
    app["source"] = source
    app["update_rate_hz"] = rate
    app.router.add_get("/", index_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get(ws_path, ws_handler)
    # Serve any static assets in ui/
    app.router.add_static("/", UI_DIR, show_index=False)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("=" * 50)
    logger.info("350Z DASH — mode=%s", source.mode)
    logger.info("  Dashboard: http://%s:%d", host, port)
    logger.info("  WebSocket: ws://%s:%d%s", host, port, ws_path)
    logger.info("=" * 50)

    # Wait for shutdown signal
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    logger.info("Shutting down...")
    await source.stop()
    await runner.cleanup()


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
