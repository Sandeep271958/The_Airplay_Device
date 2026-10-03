#!/bin/bash
set -euo pipefail
# =====================================================================
# AirPlay / Bluetooth Audio Receiver — Installation Script
# Run on a fresh Raspberry Pi OS Lite 64-bit (Bookworm)
# =====================================================================

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "======================================================="
echo "  AirPlay / Bluetooth Audio Receiver — Setup"
echo "======================================================="

# --- 1. System update ---
echo ""
echo "[1/9] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# --- 2. Audio stack ---
echo ""
echo "[2/9] Installing audio packages..."
sudo apt install -y \
    shairport-sync \
    bluez \
    bluez-alsa-utils \
    alsa-utils \
    playerctl

# --- 3. CAVA (audio visualiser engine) ---
echo ""
echo "[3/9] Installing CAVA..."
sudo apt install -y cava

# --- 4. Python & GPIO ---
echo ""
echo "[4/9] Installing Python packages..."
sudo apt install -y \
    python3-pip \
    python3-venv \
    python3-gpiozero \
    python3-rpi-lgpio \
    python3-numpy \
    fonts-dejavu-core \
    wireless-tools

# --- 5. Python virtual environment ---
echo ""
echo "[5/9] Creating Python virtual environment..."
# --system-site-packages gives access to gpiozero etc. from apt
python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install rpi_ws281x pillow spidev st7789

# --- 6. ALSA Loopback kernel module ---
echo ""
echo "[6/9] Enabling ALSA loopback module (snd-aloop)..."
if ! grep -q "^snd-aloop" /etc/modules 2>/dev/null; then
    echo "snd-aloop" | sudo tee -a /etc/modules > /dev/null
    echo "       Added snd-aloop to /etc/modules"
fi
sudo modprobe snd-aloop 2>/dev/null || true

# --- 7. Configuration files ---
echo ""
echo "[7/9] Installing configuration files..."

# ALSA routing (loopback tee + softvol)
sudo cp "$INSTALL_DIR/asound.conf" /etc/asound.conf
echo "       → /etc/asound.conf"

# Shairport-Sync (AirPlay receiver) — patch hook paths to actual install dir
sed "s|/home/pi/Airplaydevice|$INSTALL_DIR|g" \
    "$INSTALL_DIR/shairport-sync.conf" | sudo tee /etc/shairport-sync.conf > /dev/null
echo "       → /etc/shairport-sync.conf (paths patched)"

# --- 8. Hook scripts ---
echo ""
echo "[8/9] Setting up hook scripts..."
chmod +x "$INSTALL_DIR/hooks/"*.sh 2>/dev/null || true
echo "       → hooks/ made executable"

# --- 9. Systemd services ---
echo ""
echo "[9/9] Installing systemd services..."

# Patch the service files with the actual install directory
sed "s|/home/pi/Airplaydevice|$INSTALL_DIR|g" \
    "$INSTALL_DIR/airplaydevice.service" | sudo tee /etc/systemd/system/airplaydevice.service > /dev/null

sed "s|/home/pi/Airplaydevice|$INSTALL_DIR|g" \
    "$INSTALL_DIR/alsaloop.service" | sudo tee /etc/systemd/system/alsaloop.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable shairport-sync.service
sudo systemctl enable alsaloop.service
sudo systemctl enable airplaydevice.service

echo "       → airplaydevice.service enabled"
echo "       → alsaloop.service enabled"
echo "       → shairport-sync.service enabled"

# =====================================================================
echo ""
echo "======================================================="
echo "  Installation complete!"
echo "======================================================="
echo ""
echo "  MANUAL STEPS REQUIRED:"
echo ""
echo "  1. Enable SPI:"
echo "     sudo raspi-config → Interface Options → SPI → Enable"
echo ""
echo "  2. Edit /boot/firmware/config.txt — add these lines:"
echo "     ─────────────────────────────────────────"
cat "$INSTALL_DIR/boot_config.txt"
echo "     ─────────────────────────────────────────"
echo ""
echo "  3. Reboot:"
echo "     sudo reboot"
echo ""
echo "  After reboot everything starts automatically."
echo "  Check status with:"
echo "     sudo systemctl status airplaydevice"
echo "     sudo systemctl status alsaloop"
echo "     sudo systemctl status shairport-sync"
echo "======================================================="
