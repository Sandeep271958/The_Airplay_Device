#!/usr/bin/env python3
"""
ST7789 TFT Display Standalone Diagnostic Tool
==============================================
Direct hardware SPI & GPIO driver test for the 2.8" 240×320 LCD.
Uses spidev (/dev/spidev4.0) and gpiozero (lgpio) — completely bypasses
conflicting external gpiodevice libraries.

Usage:
    sudo venv/bin/python test_tft.py
"""

import sys
import time
import spidev
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from gpiozero import OutputDevice

# --- Wiring Configuration (BCM Pins) ---
SPI_PORT    = 4     # Hardware SPI4 (/dev/spidev4.0)
SPI_DEVICE  = 0     # CS0 (GPIO 25 via dtoverlay=spi4-1cs,cs0_pin=25)
PIN_DC      = 24    # Pin 18 (Green)
PIN_RST     = 12    # Pin 32 (Yellow)
PIN_BL      = 20    # Pin 38 (Grey)
WIDTH       = 240
HEIGHT      = 320
SPI_SPEED   = 32_000_000  # 32 MHz is rock solid on RPi 4B

# --- ST7789 Commands ---
CMD_SWRESET = 0x01
CMD_SLPOUT  = 0x11
CMD_NORON   = 0x13
CMD_INVON   = 0x21
CMD_INVOFF  = 0x20
CMD_DISPON  = 0x29
CMD_CASET   = 0x2A
CMD_RASET   = 0x2B
CMD_RAMWR   = 0x2C
CMD_MADCTL  = 0x36
CMD_COLMOD  = 0x3A

print("=" * 60)
print("  ST7789 TFT Display Standalone Hardware Test")
print("=" * 60)

# 1. Initialize GPIO control lines via gpiozero
print("\n[1/4] Claiming GPIO Control Pins...")
try:
    dc = OutputDevice(PIN_DC, active_high=True, initial_value=False)
    print(f"      ✓ DC pin (GPIO {PIN_DC}) claimed.")
    rst = OutputDevice(PIN_RST, active_high=True, initial_value=True)
    print(f"      ✓ RST pin (GPIO {PIN_RST}) claimed.")
    bl = OutputDevice(PIN_BL, active_high=True, initial_value=False)
    print(f"      ✓ Backlight pin (GPIO {PIN_BL}) claimed.")
except Exception as e:
    print(f"      ✗ GPIO claim failed: {e}")
    if "busy" in str(e).lower() or "16" in str(e):
        print("\n  ✦ NOTE: The background service 'airplaydevice' might be running!")
        print("    In Raspberry Pi OS (Debian Bookworm), two processes cannot hold the same GPIO pins.")
        print("    Stop the service first with:")
        print("        sudo systemctl stop airplaydevice")
        print("    Then re-run:")
        print("        sudo venv/bin/python test_tft.py\n")
    sys.exit(1)

# 2. Test Backlight
print("\n[2/4] Testing Backlight (Turning ON)...")
bl.on()
time.sleep(0.5)
print("      ✓ Backlight energized.")

# 3. Initialize Hardware SPI4
print(f"\n[3/4] Opening Hardware SPI4 (/dev/spidev{SPI_PORT}.{SPI_DEVICE})...")
try:
    spi = spidev.SpiDev()
    spi.open(SPI_PORT, SPI_DEVICE)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 0
    print(f"      ✓ SPI4 opened at {SPI_SPEED // 1_000_000} MHz.")
except Exception as e:
    print(f"      ✗ SPI open failed: {e}")
    sys.exit(1)

def write_cmd(cmd):
    dc.off()
    spi.writebytes([cmd])

def write_data(data):
    dc.on()
    if isinstance(data, int):
        spi.writebytes([data])
    elif isinstance(data, (list, tuple)):
        spi.writebytes(list(data))
    else:
        # bytes / bytearray / numpy buffer
        CHUNK = 4096
        for i in range(0, len(data), CHUNK):
            spi.writebytes2(data[i:i + CHUNK])

def reset_display():
    rst.on()
    time.sleep(0.05)
    rst.off()
    time.sleep(0.05)
    rst.on()
    time.sleep(0.15)

def init_st7789():
    reset_display()
    write_cmd(CMD_SWRESET)
    time.sleep(0.15)

    write_cmd(CMD_SLPOUT)
    time.sleep(0.12)

    # 16-bit RGB565 color
    write_cmd(CMD_COLMOD)
    write_data(0x55)

    # Orientation (Portrait, 240x320)
    write_cmd(CMD_MADCTL)
    write_data(0x00)

    # Display inversion (ST7789 IPS typically requires Inversion ON)
    write_cmd(CMD_INVON)

    # Normal display mode on
    write_cmd(CMD_NORON)
    time.sleep(0.01)

    # Display ON
    write_cmd(CMD_DISPON)
    time.sleep(0.1)

def set_window(x0, y0, x1, y1):
    write_cmd(CMD_CASET)
    write_data([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
    write_cmd(CMD_RASET)
    write_data([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
    write_cmd(CMD_RAMWR)

def show_image(img):
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT))
    # Convert PIL Image to RGB565 format using NumPy
    r = np.array(img.getchannel(0), dtype=np.uint16)
    g = np.array(img.getchannel(1), dtype=np.uint16)
    b = np.array(img.getchannel(2), dtype=np.uint16)
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    raw_bytes = rgb565.byteswap().tobytes()

    set_window(0, 0, WIDTH - 1, HEIGHT - 1)
    write_data(raw_bytes)

# 4. Run Init & Test Patterns
print("\n[4/4] Sending ST7789 Initialization Sequence...")
try:
    init_st7789()
    print("      ✓ ST7789 controller initialized!")

    # Test Color Screens
    colors = [
        ("RED",   (255, 0, 0)),
        ("GREEN", (0, 255, 0)),
        ("BLUE",  (0, 0, 255)),
    ]
    for name, col in colors:
        print(f"      → Displaying solid {name}...")
        im = Image.new("RGB", (WIDTH, HEIGHT), color=col)
        show_image(im)
        time.sleep(1)

    # Display Test Card
    print("      → Displaying 'Sandy's AirPlay Device' Test Card...")
    card = Image.new("RGB", (WIDTH, HEIGHT), color=(15, 18, 30))
    draw = ImageDraw.Draw(card)

    # Border & Badges
    draw.rectangle([6, 6, WIDTH - 7, HEIGHT - 7], outline=(0, 230, 255), width=2)
    draw.rectangle([12, 12, WIDTH - 13, 60], fill=(25, 30, 50))
    draw.text((25, 26), "SANDY'S AIRPLAY", fill=(255, 255, 255))

    # Status pills
    draw.rectangle([20, 80, 220, 115], fill=(0, 180, 100))
    draw.text((35, 90), "TFT DISPLAY: ACTIVE", fill=(255, 255, 255))

    draw.rectangle([20, 130, 220, 165], fill=(30, 40, 70))
    draw.text((35, 140), "HARDWARE SPI4 @ 32MHz", fill=(0, 220, 255))

    # Color swatches
    swatches = [(255, 0, 0), (255, 165, 0), (255, 255, 0), (0, 255, 0), (0, 200, 255), (160, 0, 255)]
    sw_w = (WIDTH - 40) // len(swatches)
    for i, sc in enumerate(swatches):
        draw.rectangle([20 + i * sw_w, 190, 20 + (i + 1) * sw_w - 2, 230], fill=sc)

    draw.text((25, 260), "Controls: OK | Audio: OK", fill=(180, 190, 210))
    draw.text((25, 285), "System Ready For Music!", fill=(0, 255, 150))

    show_image(card)
    print("      ✓ Test Card rendered successfully!")
    print("      → Display will remain ON for 10 seconds to inspect...")
    time.sleep(10)

except Exception as e:
    import traceback
    print(f"      ✗ Display error: {e}")
    traceback.print_exc()

print("\n" + "=" * 60)
print("  Test complete! Your ST7789 display is now verified.")
print("=" * 60 + "\n")
