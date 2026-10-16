#!/usr/bin/env python3
"""
WS2812B Hardware & SPI Timing Diagnostic Tool
==============================================
Tests and validates:
  1. Linux kernel core_freq locking (prevents SPI bit stretching on RPi 4B)
  2. /dev/spidev0.0 availability and permissions
  3. Daisy-chain hardware layout:
       RPi Pin 19 (GPIO 10 / SPI0 MOSI) ──→ 8×8 Matrix (Pixels 0–63)
                                                  │
                                            8×8 Matrix DOUT
                                                  │
                                                  ↓
                                            Status LED (Pixel 64)
  4. Instant thermal blanking (turns all LEDs completely OFF)
  5. Step-by-step color verification (Status LED + Matrix columns)

Usage:
  sudo python3 ws2812_diagnostic.py
"""

import sys
import os
import time
import subprocess

print("=" * 68)
print("  WS2812B LED MATRIX & STATUS INDICATOR HARDWARE DIAGNOSTIC")
print("=" * 68)

# 1. Root check
if os.geteuid() != 0:
    print("\n[!] WARNING: Not running as root (sudo).")
    print("    SPI hardware access typically requires root permissions.")
    print("    Recommended: sudo python3 ws2812_diagnostic.py\n")

# 2. Check for running background airplaydevice service
try:
    svc_status = subprocess.run(
        ["systemctl", "is-active", "airplaydevice"],
        capture_output=True, text=True, timeout=2
    ).stdout.strip()
    if svc_status == "active":
        print("[!] NOTICE: 'airplaydevice' service is currently running in background.")
        print("    It may be actively writing to the LEDs concurrently.")
        print("    To stop it during this test: sudo systemctl stop airplaydevice\n")
except Exception:
    pass

# 3. Inspect /boot/firmware/config.txt or /boot/config.txt for core_freq
cfg_paths = ["/boot/firmware/config.txt", "/boot/config.txt"]
cfg_found = None
core_freq_locked = False
spi_enabled = False

for path in cfg_paths:
    if os.path.exists(path):
        cfg_found = path
        with open(path, "r") as f:
            content = f.read()
            if "core_freq=500" in content:
                core_freq_locked = "500MHz (RPi 4B)"
            elif "core_freq=400" in content:
                core_freq_locked = "400MHz (RPi Zero 2 W)"
            if "dtparam=spi=on" in content or "dtoverlay=spi0-1cs" in content:
                spi_enabled = True
        break

print("[DIAGNOSTIC 1/5] Boot Firmware Configuration:")
if cfg_found:
    print(f"  ✓ Configuration file found: {cfg_found}")
    if core_freq_locked:
        print(f"  ✓ GPU core_freq is locked to {core_freq_locked} (SPI timing is locked & protected)")
    else:
        print("  ✗ CRITICAL: 'core_freq=500' or 'core_freq=400' NOT detected in config.txt!")
        print("    → Dynamic CPU/GPU clock scaling stretches SPI bits,")
        print("      causing WS2812B LEDs to decode every bit as 1 (blazing white & overheating).")
        print("    → Fix: Run sudo ./install.sh to automatically inject hardware overlays.")

    if spi_enabled:
        print("  ✓ SPI interface is enabled")
    else:
        print("  ✗ SPI interface (dtparam=spi=on) not found in config.txt")
else:
    print("  ! config.txt not found at standard boot paths (non-RPi or custom mount)")

# 4. Check live GPU core clock via vcgencmd
print("\n[DIAGNOSTIC 2/5] Live Core Clock Reading:")
try:
    vc_out = subprocess.run(["vcgencmd", "measure_clock", "core"],
                            capture_output=True, text=True, timeout=2).stdout.strip()
    print(f"  → Current GPU Core Clock: {vc_out}")
except Exception as e:
    print(f"  ! Unable to query vcgencmd: {e}")

# 5. Check SPI device node
print("\n[DIAGNOSTIC 3/5] SPI Device Node (/dev/spidev0.0):")
if os.path.exists("/dev/spidev0.0"):
    print("  ✓ /dev/spidev0.0 exists and is accessible")
else:
    print("  ✗ /dev/spidev0.0 NOT found!")
    print("    → Enable SPI: sudo raspi-config → Interface Options → SPI → Enable")

# 6. Initialize rpi_ws281x
print("\n[DIAGNOSTIC 4/5] Initializing WS2812B via rpi_ws281x:")
try:
    import rpi_ws281x as ws
except ImportError:
    # Check if project virtual environment python exists
    venv_py = os.path.expanduser("~/Airplaydevice/venv/bin/python")
    if not os.path.exists(venv_py):
        venv_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python")
    if os.path.exists(venv_py) and sys.executable != venv_py:
        print(f"  → Switching to project virtualenv Python: {venv_py}\n")
        os.execv(venv_py, [venv_py] + sys.argv)
    else:
        print("  ✗ rpi_ws281x module not installed in current Python environment!")
        print("    Install: ~/Airplaydevice/venv/bin/pip install rpi-ws281x")
        sys.exit(1)

LED_COUNT        = 65                    # 64 matrix LEDs + 1 status LED
LED_PIN          = 10                    # GPIO 10 (SPI0 MOSI / Physical Pin 19)
LED_FREQ_HZ      = 800000                # 800 kHz protocol
LED_DMA          = 10
LED_BRIGHTNESS   = 35                    # Gentle brightness to prevent heating
LED_INVERT       = False
LED_CHANNEL      = 0
LED_STRIP_TYPE   = ws.WS2811_STRIP_GRB

try:
    strip = ws.Adafruit_NeoPixel(
        LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
        LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL, LED_STRIP_TYPE,
    )
    strip.begin()
    strip.setBrightness(LED_BRIGHTNESS)
    print(f"  ✓ Strip initialized (65 pixels on GPIO 10 / Pin 19, Brightness={LED_BRIGHTNESS})")
except Exception as e:
    print(f"  ✗ FATAL: Failed to initialize rpi_ws281x: {e}")
    sys.exit(1)

# Helper colors
OFF   = ws.Color(0, 0, 0)
RED   = ws.Color(255, 0, 0)
GREEN = ws.Color(0, 255, 0)
BLUE  = ws.Color(0, 0, 255)
AMBER = ws.Color(255, 120, 0)
WHITE = ws.Color(255, 255, 255)

print("\n[DIAGNOSTIC 5/5] Executing Hardware Visual Test Sequence:")

# Test Step A: Instant Blanking
print("  [Step A] Blanking all 65 pixels (Matrix + Status LED)...")
for i in range(LED_COUNT):
    strip.setPixelColor(i, OFF)
strip.show()
time.sleep(1.0)
print("    → Check hardware: ALL 64 matrix pixels and Status LED should be completely OFF (dark).")
print("    → If LEDs are STILL glowing white, the SPI clock is fluctuating (missing core_freq=500)")
print("      or the Ground (GND) wire between the Pi and external PSU is disconnected.")

# Test Step B: Dedicated Status LED (Pixel 64 on DOUT)
print("\n  [Step B] Testing Status LED (Pixel 64, connected to Matrix DOUT)...")
print("    → Status LED: RED for 1.5s")
strip.setPixelColor(64, RED)
strip.show()
time.sleep(1.5)

print("    → Status LED: GREEN for 1.5s")
strip.setPixelColor(64, GREEN)
strip.show()
time.sleep(1.5)

print("    → Status LED: BLUE for 1.5s")
strip.setPixelColor(64, BLUE)
strip.show()
time.sleep(1.5)

strip.setPixelColor(64, OFF)
strip.show()

# Test Step C: 8x8 Matrix Column Sweep (Pixels 0–63)
print("\n  [Step C] Testing 8x8 Matrix Columns (Pixels 0–63, Progressive layout)...")
for col in range(8):
    # Blank matrix
    for i in range(64):
        strip.setPixelColor(i, OFF)
    # Light current column in progressive order (bottom -> top)
    for row in range(8):
        idx = col * 8 + row
        strip.setPixelColor(idx, AMBER)
    strip.show()
    print(f"    → Column {col} lit (pixels {col*8}..{col*8+7}, bottom -> top)")
    time.sleep(0.35)

# Test Step D: Clean shutdown
print("\n  [Step D] Final Blanking (Zero Watts / Thermal Safe)...")
for i in range(LED_COUNT):
    strip.setPixelColor(i, OFF)
strip.show()
print("    ✓ All pixels powered OFF.")

print("\n" + "=" * 68)
print("  DIAGNOSTIC COMPLETE")
print("=" * 68)
