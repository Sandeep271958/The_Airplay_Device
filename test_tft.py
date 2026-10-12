#!/usr/bin/env python3
"""
TFT Display Diagnostic & Test Utility
======================================
Tests the ST7789 2.8" LCD module wiring, backlight, and rendering.

Usage:
    sudo venv/bin/python test_tft.py
"""

import os
import sys
import time
from glob import glob

print("=" * 60)
print("  ST7789 TFT Display Diagnostic Tool")
print("=" * 60)

# 1. Inspect available SPI devices in Linux
spi_devices = glob("/dev/spidev*")
print(f"\n[1/3] Detected SPI Devices in /dev/:")
if spi_devices:
    for dev in sorted(spi_devices):
        print(f"      ✓ {dev}")
else:
    print("      ✗ None found! Is SPI enabled in /boot/firmware/config.txt?")

# 2. Test Backlight Pin (GPIO 20)
PIN_BL = 20
print(f"\n[2/3] Testing Backlight (GPIO {PIN_BL}, Physical Pin 38)...")
try:
    from gpiozero import OutputDevice
    bl = OutputDevice(PIN_BL, active_high=True, initial_value=False)
    print("      Turning backlight ON for 3 seconds...")
    bl.on()
    time.sleep(3)
    bl.off()
    print("      Turning backlight OFF...")
    bl.close()
    print("      ✓ Backlight test completed.")
except Exception as e:
    print(f"      ✗ Backlight test error: {e}")

# 3. Test Display Initialization & Drawing
print(f"\n[3/3] Testing ST7789 Display Driver...")

# Try port 4 first (RPi 4B hardware SPI4), then port 1
target_port = None
target_cs = 0

for p in [4, 1]:
    if os.path.exists(f"/dev/spidev{p}.0"):
        target_port = p
        target_cs = 0
        break
    elif os.path.exists(f"/dev/spidev{p}.1"):
        target_port = p
        target_cs = 1
        break

if target_port is None:
    print("      ✗ /dev/spidev4.0 not found yet!")
    print("\n  TO FIX:")
    print("  1. In /boot/firmware/config.txt, add:")
    print("         dtoverlay=spi4-1cs,cs0_pin=25")
    print("  2. Ensure the purple SCK wire is on Pin 26 (GPIO 7).")
    print("  3. Save the file and reboot: sudo reboot")
    print("\n" + "=" * 60)
    sys.exit(1)

try:
    import st7789
    from PIL import Image, ImageDraw, ImageFont

    print(f"      Connecting via /dev/spidev{target_port}.{target_cs}...")
    disp = st7789.ST7789(
        port=target_port,
        cs=target_cs,
        dc=24,
        rst=12,
        backlight=20,
        width=240,
        height=320,
        rotation=0,
        spi_speed_hz=40_000_000,
    )
    if hasattr(disp, "set_backlight"):
        disp.set_backlight(True)

    # Test Pattern: Solid Red, then Green, then Blue, then Sandy's AirPlay logo
    colors = [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255))]
    for name, col in colors:
        print(f"      Drawing solid {name} screen...")
        img = Image.new("RGB", (240, 320), color=col)
        disp.display(img)
        time.sleep(1)

    # Info screen
    img = Image.new("RGB", (240, 320), color=(20, 20, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 230, 310], outline=(0, 255, 200), width=2)
    draw.text((30, 50), "SANDY'S AIRPLAY", fill=(255, 255, 255))
    draw.text((30, 80), "DEVICE ACTIVE", fill=(0, 255, 128))
    draw.text((30, 130), "TFT ST7789: OK", fill=(255, 200, 0))
    draw.text((30, 160), f"SPI Bus: port={target_port}", fill=(180, 180, 180))
    disp.display(img)
    print("      ✓ Display test pattern rendered successfully!")

except Exception as e:
    print(f"      ✗ ST7789 initialization/draw failed: {e}")

print("\n" + "=" * 60)
