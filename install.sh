#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# install.sh — Gate Access Monitoring System installer for Raspberry Pi OS
# ═══════════════════════════════════════════════════════════════════════════════
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# What it does:
#   1. Installs system dependencies (Tk, fonts, etc.)
#   2. Creates a Python virtual environment
#   3. Installs Python packages from requirements.txt
#   4. Copies the systemd service (optional)
#   5. Creates config.env from the example if missing
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

echo "═══════════════════════════════════════════════════"
echo " Gate Access Monitoring System — Installer"
echo "═══════════════════════════════════════════════════"
echo ""

# ── 1. System packages ───────────────────────────────────────────────────────
echo "[1/5] Installing system dependencies…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-pip python3-tk \
    fonts-amiri \
    libatlas-base-dev libjpeg-dev zlib1g-dev \
    > /dev/null 2>&1
echo "      ✓ System packages installed"

# ── 2. Virtual environment ───────────────────────────────────────────────────
echo "[2/5] Creating Python virtual environment…"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
echo "      ✓ venv at $VENV_DIR"

# ── 3. Python packages ──────────────────────────────────────────────────────
echo "[3/5] Installing Python packages…"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
echo "      ✓ Python packages installed"

# ── 4. Config file ──────────────────────────────────────────────────────────
echo "[4/5] Checking config.env…"
if [ ! -f "$SCRIPT_DIR/config.env" ]; then
    cp "$SCRIPT_DIR/config.env.example" "$SCRIPT_DIR/config.env"
    echo "      ✓ Created config.env from example — edit it with your settings"
else
    echo "      ✓ config.env already exists (skipped)"
fi

# ── 5. systemd service (optional) ───────────────────────────────────────────
echo "[5/5] systemd service…"
read -rp "      Install as a systemd service (auto-start on boot)? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
    # Update paths in the service file to match the install location
    SERVICE_FILE="/etc/systemd/system/gate-scanner.service"
    sed \
        -e "s|/home/pi/gate-scanner|$SCRIPT_DIR|g" \
        -e "s|User=pi|User=$(whoami)|g" \
        "$SCRIPT_DIR/gate-scanner.service" | sudo tee "$SERVICE_FILE" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable gate-scanner.service
    echo "      ✓ Service installed and enabled"
    echo "        Start now:  sudo systemctl start gate-scanner"
    echo "        View logs:  journalctl -u gate-scanner -f"
else
    echo "      ✓ Skipped (you can install it later manually)"
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo " Installation complete!"
echo ""
echo " Quick start:"
echo "   1. Edit config.env with your API URL and keys"
echo "   2. source config.env"
echo "   3. $VENV_DIR/bin/python gate.py"
echo "═══════════════════════════════════════════════════"
