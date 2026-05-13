#!/usr/bin/env bash
# =====================================================================
# 350Z Dash — Raspberry Pi installer
#
# Tested on: Raspberry Pi OS Lite Bookworm (64-bit), Raspberry Pi 4 / 5
#
# What this does:
#   1. Installs system packages (Python, Chromium, X server, etc.)
#   2. Installs Python dependencies into a venv inside this project
#   3. Adds the current user to the `dialout` group for /dev/ttyUSB access
#   4. Installs a systemd service that runs the dash server at boot
#   5. Configures auto-login on tty1
#   6. Configures Chromium kiosk to auto-launch on login
#
# Usage (from inside this directory):
#   bash scripts/install-pi.sh
#
# To uninstall:
#   sudo systemctl disable --now 350z-dash.service
#   sudo rm /etc/systemd/system/350z-dash.service
#   sudo raspi-config  # turn off auto-login
# =====================================================================

set -euo pipefail

# ---- Sanity checks ---------------------------------------------------
if [[ "${EUID}" -eq 0 ]]; then
  echo "Don't run this script as root. It'll sudo what it needs."
  exit 1
fi

if ! grep -qi "raspberry\|debian" /etc/os-release; then
  echo "This installer targets Raspberry Pi OS / Debian. Detected:"
  cat /etc/os-release
  read -p "Continue anyway? [y/N] " yn
  [[ "${yn,,}" == "y" ]] || exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
USER_NAME="$(whoami)"
HOME_DIR="${HOME}"

echo "==> Project dir:  ${PROJECT_DIR}"
echo "==> Installing as: ${USER_NAME}"

# ---- System packages -------------------------------------------------
echo
echo "==> Installing system packages..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3 \
  python3-venv \
  python3-pip \
  python3-dev \
  chromium-browser \
  xserver-xorg \
  xinit \
  x11-xserver-utils \
  matchbox-window-manager \
  unclutter \
  fonts-dejavu-core

# ---- Python venv -----------------------------------------------------
echo
echo "==> Creating Python venv..."
cd "${PROJECT_DIR}"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

# ---- Serial port group access ---------------------------------------
echo
echo "==> Adding ${USER_NAME} to dialout group (for /dev/ttyUSB*)..."
sudo usermod -a -G dialout "${USER_NAME}"

# ---- Auto-login on tty1 ---------------------------------------------
echo
echo "==> Configuring auto-login on tty1..."
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/override.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${USER_NAME} --noclear %I \$TERM
EOF

# ---- Systemd service for the dash server ----------------------------
echo
echo "==> Installing systemd service..."
sudo tee /etc/systemd/system/350z-dash.service >/dev/null <<EOF
[Unit]
Description=350Z Dash Telemetry Server
After=network.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/python -m server.main
Restart=on-failure
RestartSec=3

# Don't crash the Pi if the dash server hits an unrecoverable error
StartLimitInterval=60
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable 350z-dash.service

# ---- Kiosk launcher --------------------------------------------------
echo
echo "==> Installing kiosk launcher..."

# .xinitrc launches the browser when startx runs
cat > "${HOME_DIR}/.xinitrc" <<'EOF'
#!/bin/sh
# Display + cursor management
xset -dpms          # no power saving
xset s off          # no screensaver
xset s noblank
unclutter -idle 0.1 -root &
matchbox-window-manager -use_titlebar no &

# Wait for the dash server to be ready before launching the browser.
# Up to 30s — typically it's ready in 2-3.
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8080/health >/dev/null 2>&1; then break; fi
  sleep 1
done

exec chromium-browser \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-translate \
  --disable-features=TranslateUI \
  --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 \
  --disable-pinch \
  --autoplay-policy=no-user-gesture-required \
  --app=http://127.0.0.1:8080/
EOF
chmod +x "${HOME_DIR}/.xinitrc"

# .bash_profile triggers startx automatically on tty1 login
PROFILE="${HOME_DIR}/.bash_profile"
if ! grep -q "350Z DASH AUTOSTART" "${PROFILE}" 2>/dev/null; then
  cat >> "${PROFILE}" <<'EOF'

# === 350Z DASH AUTOSTART ===
if [ -z "${DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then
  exec startx
fi
EOF
fi

# ---- Done ------------------------------------------------------------
echo
echo "================================================================"
echo " Install complete."
echo
echo " The dash server will start on boot. Chromium kiosk will follow."
echo
echo " IMPORTANT: log out and back in (or reboot) so dialout group"
echo " membership takes effect, then plug in your Consult-II cable."
echo
echo " Manual testing without reboot:"
echo "   cd ${PROJECT_DIR}"
echo "   source .venv/bin/activate"
echo "   python -m server.main --simulator"
echo "   # then open http://localhost:8080 in a browser"
echo
echo " Reboot when ready:    sudo reboot"
echo " Watch service logs:   journalctl -u 350z-dash -f"
echo "================================================================"
