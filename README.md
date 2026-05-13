# 350Z Dash

A standalone, neon-green-CRT-styled telemetry dash for a 2003+ Nissan 350Z (VQ35DE).
Reads live engine data from the ECU via a Consult-II cable on the OBD-II port,
streams it over a local WebSocket, and renders a dense gauge UI in the browser.

Designed to run on a Raspberry Pi 4/5 with a 7" touchscreen mounted in the cabin.

```
+-------------+      +-------------+      +--------------+      +---------+
|  350Z ECU   | ---> |  KKL cable  | ---> |  Pi 5 + .py  | ---> | Browser |
| (Consult-II)|      |  USB serial |      |  WebSocket   |      | (kiosk) |
+-------------+      +-------------+      +--------------+      +---------+
```

## Quickstart — laptop dev mode

You don't need a Pi or a cable to develop the UI. The server can run in simulator
mode on any machine with Python 3.11+.

```bash
git clone <this-repo>
cd 350z-dash
bash scripts/run-dev.sh
```

Open `http://localhost:8080`. You'll see the dash with the simulator running a
30-second acceleration → cruise → brake loop.

If you've got the Consult-II cable plugged into your laptop, you can test it too:

```bash
bash scripts/run-dev.sh --consult         # try real ECU (will fail without one)
bash scripts/run-dev.sh --probe           # one-shot diagnostic dump
bash scripts/run-dev.sh --port /dev/ttyUSB0  # override auto-detect
```

## Quickstart — Raspberry Pi install

On a fresh Raspberry Pi OS Lite (Bookworm) install:

```bash
git clone <this-repo>
cd 350z-dash
bash scripts/install-pi.sh
sudo reboot
```

After reboot, the Pi will auto-login on tty1, the dash server will start as a
systemd service, and Chromium will launch in kiosk mode pointing at the dash.

The installer:
- Adds your user to the `dialout` group (for USB serial access)
- Installs Python, Chromium, X11, and minimal window manager
- Creates a `.venv` with the project's Python deps
- Installs `350z-dash.service` (auto-starts on boot)
- Configures auto-login + auto-startx + auto-kiosk

## Hardware

| Item | Source | Notes |
|---|---|---|
| Raspberry Pi 4 or 5 (8GB) | Core Electronics / Little Bird | The 5 has more headroom |
| 7" touchscreen | Official Pi screen for prototyping; automotive 1000+ nit for production | |
| Consult-II cable | VAG KKL FTDI on eBay AU (~$15–25) | Ensure FT232R chip — CH340 is unreliable under engine noise |
| Car-PC power supply | Mausberry or similar | Senses ignition, graceful shutdown |
| 3D-printed mount | FDM for housing | Resin for connector adapters |

## Configuration

Edit `config.yaml` to change defaults without touching code:

- `server.host` — `127.0.0.1` (default) or `0.0.0.0` to access from your phone
- `server.update_rate_hz` — how fast to push data to the browser (default 15Hz)
- `serial.port` — explicit port (otherwise auto-detect)
- `mode.default` — `auto` (try ECU, fall back to sim), `simulator`, or `consult`
- `logging.csv_path` — set a path to log every frame to CSV for analysis

## Project structure

```
350z-dash/
├── config.yaml              # User-editable settings
├── requirements.txt
├── server/
│   ├── main.py              # HTTP + WebSocket server entry point
│   ├── consult.py           # Consult-II protocol implementation
│   ├── registers.py         # ECU register address map
│   ├── simulator.py         # Server-side simulator (laptop dev)
│   └── serial_io.py         # USB serial auto-detect
├── ui/
│   └── index.html           # Dashboard (CRT-style, vanilla HTML/JS)
└── scripts/
    ├── run-dev.sh           # Laptop quickstart
    └── install-pi.sh        # Raspberry Pi installer
```

## Honest notes on the Consult-II side

**The simulator path is fully working and tested.** The dash UI, WebSocket plumbing,
auto-detect, and config loading all work end-to-end out of the box.

**The Consult-II protocol layer is *implemented but unvalidated against real hardware*.**
This is not a fully-documented Nissan protocol — the byte-level details below come
from community work (ECUtalk, Nissan DataScan II reverse engineering, the openconsult
project). When you first plug in your cable:

1. **Confirm the cable works** at the lowest level. With ignition ON, engine OFF:
   ```bash
   python -m server.main --probe
   ```
   This sends the wake byte and looks for an echo. If you don't see "ECU handshake OK"
   you've got a hardware problem before any decoding can possibly work — likely a CH340
   chip dropping bytes, or a cable that's actually a 14-pin Consult-I clone.

2. **Validate register addresses + decode formulas.** Open `server/registers.py`.
   Every register marked `# VERIFY` is a community best-guess for the 350Z. Cross-check
   each value against another known-good tool (Nissan DataScan II software at minimum,
   or Torque Pro via OBD-II for the params both expose — coolant temp, RPM, vehicle
   speed, throttle position). Adjust the `decode=` formula until values match.

3. **RPM is a two-byte register** (high byte * 256 + low byte) and isn't yet handled
   correctly in `consult.py` — see the TODO. This is the first thing to fix once you've
   got handshake working.

4. **Oil pressure, oil temp, and accurate AFR are not on the Consult-II bus** for the
   NA VQ35DE. The dash shows placeholders for these. To get real values you'll need
   aftermarket sensors (oil pressure sender, oil temp sender, wideband O2 controller)
   feeding an ADC HAT on the Pi — that's a future module that publishes additional
   fields into the same WebSocket payload.

## Common operations

```bash
# Watch live logs (Pi)
journalctl -u 350z-dash -f

# Restart the server
sudo systemctl restart 350z-dash

# Run in foreground for debugging (stop the service first)
sudo systemctl stop 350z-dash
cd ~/350z-dash && source .venv/bin/activate
python -m server.main

# Disable the dash temporarily
sudo systemctl disable --now 350z-dash

# Exit kiosk mode on the Pi (returns to console)
Ctrl+Alt+F2  # then log in normally
```

## Network access from your phone

If you want to see the dash on your phone (useful for adjusting things from outside the
car while it's running on a bench), edit `config.yaml`:

```yaml
server:
  host: "0.0.0.0"
```

Then point your phone at `http://<pi-ip>:8080`. Only do this on a trusted network —
there's no auth.

## License

MIT — go nuts.
