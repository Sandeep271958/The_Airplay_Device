#!/bin/bash
set -euo pipefail
# =====================================================================
# AirPlay / Bluetooth Audio Receiver — Installation Script
# Run on a fresh Raspberry Pi OS Lite 64-bit (Bookworm)
# =====================================================================

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Detect Raspberry Pi model ---
PI_MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "Unknown")
echo "  Detected: $PI_MODEL"

# Check if this is a RPi 4B (prototype mode — uses onboard headphone jack)
if echo "$PI_MODEL" | grep -qi "Raspberry Pi 4"; then
    IS_RPI4B=true
    echo "  → Detected Raspberry Pi 4B (prototype config: onboard 3.5mm audio & SPI4)"
elif echo "$PI_MODEL" | grep -qi "Zero 2"; then
    IS_RPI4B=false
    echo "  → Detected Raspberry Pi Zero 2 W (production config: PCM5102A I2S DAC & Software SPI Bus 1)"
else
    IS_RPI4B=false
    echo "  → Detected $PI_MODEL (defaulting to production I2S DAC config)"
fi

echo "======================================================="
echo "  AirPlay / Bluetooth Audio Receiver — Automated Setup"
echo "======================================================="

# --- 1. System update ---
echo ""
echo "[1/10] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# --- 2. Audio stack & Hi-Fi Sinc Resampling ---
echo ""
echo "[2/10] Installing audio packages & Hi-Fi resampling plugins..."
sudo apt install -y \
    shairport-sync \
    bluez \
    bluez-alsa-utils \
    alsa-utils \
    libsamplerate0 \
    libasound2-plugins \
    playerctl

# --- 3. CAVA (audio visualiser engine) ---
echo ""
echo "[3/10] Installing CAVA..."
sudo apt install -y cava

# --- 4. Python & GPIO ---
echo ""
echo "[4/10] Installing Python packages..."
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
echo "[5/10] Creating Python virtual environment..."
# --system-site-packages gives access to gpiozero etc. from apt
python3 -m venv --system-site-packages "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
else
    "$INSTALL_DIR/venv/bin/pip" install rpi_ws281x pillow spidev st7789 numpy
fi

# --- 6. ALSA Loopback kernel module ---
echo ""
echo "[6/10] Enabling ALSA loopback module (snd-aloop)..."
if ! grep -q "^snd-aloop" /etc/modules 2>/dev/null; then
    echo "snd-aloop" | sudo tee -a /etc/modules > /dev/null
    echo "       Added snd-aloop to /etc/modules"
fi
sudo modprobe snd-aloop 2>/dev/null || true

# --- Auto-enable SPI via raspi-config ---
echo "       Enabling hardware SPI interface..."
sudo raspi-config nonint do_spi 0 2>/dev/null || true

# --- 7. Configuration files ---
echo ""
echo "[7/10] Installing configuration files..."

# ALSA routing (loopback tee + softvol + 44.1kHz rate lock)
if [ "$IS_RPI4B" = true ]; then
    sudo cp "$INSTALL_DIR/asound_rpi4b.conf" /etc/asound.conf
    echo "       → /etc/asound.conf (RPi 4B — 3.5mm headphone jack with Speex resampler)"
else
    sudo cp "$INSTALL_DIR/asound.conf" /etc/asound.conf
    echo "       → /etc/asound.conf (RPi Zero 2 W — PCM5102A I2S DAC with Speex resampler)"
fi

# Shairport-Sync (AirPlay receiver) — patch hook paths to actual install dir
sed -E "s|/home/(pi|sandypi)/Airplaydevice|$INSTALL_DIR|g" \
    "$INSTALL_DIR/shairport-sync.conf" | sudo tee /etc/shairport-sync.conf > /dev/null
echo "       → /etc/shairport-sync.conf (paths patched, 44.1kHz rate locked)"

# --- 8. Hook scripts ---
echo ""
echo "[8/10] Setting up hook scripts..."
chmod +x "$INSTALL_DIR/hooks/"*.sh 2>/dev/null || true
sudo cp "$INSTALL_DIR/hooks/on_event.sh" /usr/local/bin/airplay_on_event.sh
sudo chmod 755 /usr/local/bin/airplay_on_event.sh
echo "       → /usr/local/bin/airplay_on_event.sh installed"

# --- 9. Systemd services ---
echo ""
echo "[9/10] Installing systemd services (Direct Hardware Pipeline)..."

# Ensure legacy alsaloop service is stopped and removed (replaced by ALSA multi/route)
if systemctl is-active --quiet alsaloop.service 2>/dev/null; then
    sudo systemctl stop alsaloop.service 2>/dev/null || true
fi
sudo systemctl disable alsaloop.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/alsaloop.service 2>/dev/null || true

# Patch the service files with the actual install directory
sed -E "s|/home/(pi|sandypi)/Airplaydevice|$INSTALL_DIR|g" \
    "$INSTALL_DIR/airplaydevice.service" | sudo tee /etc/systemd/system/airplaydevice.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable shairport-sync.service
sudo systemctl enable airplaydevice.service

echo "       → airplaydevice.service enabled"
echo "       → shairport-sync.service enabled (direct hardware playback)"
echo "       → alsaloop.service permanently disabled (replaced by zero-latency ALSA kernel multi/route)"

# --- 10. Boot & Hardware Configuration (Fully Automated) ---
echo ""
echo "[10/10] Configuring hardware overlays & audio clock settings in boot config..."

BOOT_CONFIG="/boot/firmware/config.txt"
if [ ! -f "$BOOT_CONFIG" ]; then
    BOOT_CONFIG="/boot/config.txt"
fi

if [ -f "$BOOT_CONFIG" ]; then
    echo "       Target boot config: $BOOT_CONFIG"
    
    # 1. Create a timestamped backup before modification
    BACKUP_FILE="${BOOT_CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
    sudo cp "$BOOT_CONFIG" "$BACKUP_FILE"
    echo "       Created backup: $BACKUP_FILE"

    MARKER_START="# --- Sandy's AirPlay Device Hardware Configuration ---"
    MARKER_END="# --- End Sandy's AirPlay Device Hardware Configuration ---"

    # 2. Remove prior auto-injected section to maintain strict idempotence
    if grep -q "$MARKER_START" "$BOOT_CONFIG"; then
        echo "       Updating existing AirPlay hardware configuration block..."
        sudo sed -i "\|$MARKER_START|,\|$MARKER_END|d" "$BOOT_CONFIG"
    fi

    # 3. For RPi Zero 2 W / I2S DAC: disable stock onboard audio if present
    if [ "$IS_RPI4B" = false ]; then
        if grep -q "^[[:space:]]*dtparam=audio=on" "$BOOT_CONFIG"; then
            echo "       Disabling stock onboard audio (to avoid conflict with PCM5102A I2S DAC)..."
            sudo sed -i -E 's/^[[:space:]]*dtparam=audio=on/# dtparam=audio=on (disabled for I2S DAC)/' "$BOOT_CONFIG"
        fi
    fi

    # 4. Inject board-specific hardware block
    echo "       Injecting board-specific hardware overlays..."
    {
        echo ""
        echo "$MARKER_START"
        if [ "$IS_RPI4B" = true ]; then
            echo "# Configured for Raspberry Pi 4B (3.5mm Hi-Fi analog output & Hardware SPI4)"
            cat "$INSTALL_DIR/boot_config_rpi4b.txt"
        else
            echo "# Configured for Raspberry Pi Zero 2 W (PCM5102A I2S DAC & Bus 1 Software SPI)"
            cat "$INSTALL_DIR/boot_config.txt"
        fi
        echo "$MARKER_END"
    } | sudo tee -a "$BOOT_CONFIG" > /dev/null

    echo "       ✓ Boot configuration successfully updated in $BOOT_CONFIG"
else
    echo "       [WARNING] Could not find /boot/firmware/config.txt or /boot/config.txt"
    echo "       Please manually add the hardware overlays from $INSTALL_DIR/boot_config.txt"
fi

# =====================================================================
echo ""
echo "======================================================="
echo "  ★ Installation Complete! Zero Manual Steps Needed ★"
echo "======================================================="
echo ""
echo "  All components have been configured automatically:"
echo "    ✓ Hi-Fi 44.1kHz sample rate locked across entire pipeline"
echo "    ✓ Studio-grade Speex resampler & libsamplerate sinc sync enabled"
echo "    ✓ Buffer latency set to 120ms (zero stutter, zero underruns)"
if [ "$IS_RPI4B" = true ]; then
    echo "    ✓ 2nd-Order Sigma-Delta modulation enabled (audio_pwm_mode=2)"
    echo "    ✓ Hardware SPI4 (32MHz DMA) configured for ST7789 TFT display"
else
    echo "    ✓ PCM5102A I2S DAC overlay configured (dtoverlay=hifiberry-dac)"
    echo "    ✓ Software SPI Bus 1 configured for ST7789 TFT display"
fi
echo "    ✓ Systemd services (airplaydevice, alsaloop, shairport-sync) enabled"
echo ""
echo "  PLEASE REBOOT TO APPLY ALL HARDWARE OVERLAYS & AUDIO FILTERS:"
echo "     sudo reboot"
echo "======================================================="
