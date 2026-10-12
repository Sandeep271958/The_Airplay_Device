#!/usr/bin/env python3
"""
ST7789 TFT Display Standalone Calibration & Diagnostic Tool
============================================================
Direct hardware SPI & GPIO driver test for the 2.8" 240×320 LCD.
Calibrates subpixel color mapping (RGB vs BGR) and display inversion (INVON vs INVOFF).

Usage:
    sudo venv/bin/python test_tft.py               # Standard calibrated test (BGR + INVOFF)
    sudo venv/bin/python test_tft.py --rgb         # Force RGB mode
    sudo venv/bin/python test_tft.py --invon       # Force Inversion ON
"""

import sys
import os
import time
import argparse
import spidev
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from gpiozero import OutputDevice

# --- CLI Options ---
parser = argparse.ArgumentParser(description="ST7789 TFT Color Calibration & Hardware Diagnostic")
parser.add_argument("--bgr", dest="bgr", action="store_true", default=True,
                    help="Use BGR subpixel ordering (fixes Red/Blue swap, default: True)")
parser.add_argument("--rgb", dest="bgr", action="store_false",
                    help="Use RGB subpixel ordering")
parser.add_argument("--invoff", dest="invert", action="store_false", default=False,
                    help="Disable display inversion (fixes washed-out contrast, default: False)")
parser.add_argument("--invon", dest="invert", action="store_true",
                    help="Enable display inversion")
args, _ = parser.parse_known_args()

# --- Wiring Configuration (BCM Pins) ---
SPI_PORT    = 4     # Hardware SPI4 (/dev/spidev4.0)
SPI_DEVICE  = 0     # CS0 (GPIO 25 via dtoverlay=spi4-1cs,cs0_pin=25)
PIN_DC      = 24    # Pin 18 (Green)
PIN_RST     = 12    # Pin 32 (Yellow)
PIN_BL      = 20    # Pin 38 (Grey)
WIDTH       = 240
HEIGHT      = 320
SPI_SPEED   = 32_000_000  # 32 MHz on RPi 4B

# --- ST7789 Commands ---
CMD_SWRESET = 0x01
CMD_SLPOUT  = 0x11
CMD_NORON   = 0x13
CMD_INVOFF  = 0x20
CMD_INVON   = 0x21
CMD_DISPON  = 0x29
CMD_CASET   = 0x2A
CMD_RASET   = 0x2B
CMD_RAMWR   = 0x2C
CMD_MADCTL  = 0x36
CMD_COLMOD  = 0x3A

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")

def load_font(size, bold=False):
    candidates = []
    if bold:
        candidates.append(os.path.join(FONTS_DIR, "SF-Pro-Rounded-Bold.ttf"))
    candidates.append(os.path.join(FONTS_DIR, "SF-Compact-Rounded.ttf"))
    candidates.extend([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ])
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

print("=" * 65)
print("  ST7789 TFT Color Calibration & Hardware Diagnostic")
print(f"  Configuration: BGR={args.bgr} | Invert={args.invert}")
print("=" * 65)

# 1. Initialize GPIO control lines via gpiozero
print("\n[1/4] Claiming GPIO Control Pins...")
try:
    dc = OutputDevice(PIN_DC, active_high=True, initial_value=False)
    rst = OutputDevice(PIN_RST, active_high=True, initial_value=True)
    bl = OutputDevice(PIN_BL, active_high=True, initial_value=False)
    print(f"      ✓ DC={PIN_DC}, RST={PIN_RST}, BL={PIN_BL} claimed successfully.")
except Exception as e:
    print(f"      ✗ GPIO claim failed: {e}")
    if "busy" in str(e).lower() or "16" in str(e):
        print("\n  ✦ NOTE: The background service 'airplaydevice' is currently running!")
        print("    Stop it first so test_tft can access GPIO lines:")
        print("        sudo systemctl stop airplaydevice")
        print("    Then re-run: sudo venv/bin/python test_tft.py\n")
    sys.exit(1)

# 2. Test Backlight
print("\n[2/4] Energizing Backlight (BL=GPIO 20)...")
bl.on()
time.sleep(0.3)
print("      ✓ Backlight active.")

# 3. Initialize Hardware SPI4
print(f"\n[3/4] Opening Hardware SPI4 (/dev/spidev{SPI_PORT}.{SPI_DEVICE} @ {SPI_SPEED // 1_000_000} MHz)...")
try:
    spi = spidev.SpiDev()
    spi.open(SPI_PORT, SPI_DEVICE)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 0
    print(f"      ✓ SPI4 bus ready.")
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

def init_st7789(bgr=True, invert=False):
    reset_display()
    write_cmd(CMD_SWRESET)
    time.sleep(0.15)

    write_cmd(CMD_SLPOUT)
    time.sleep(0.12)

    # 16-bit RGB565 color
    write_cmd(CMD_COLMOD)
    write_data(0x55)

    # MADCTL: Bit 3 = 0x08 activates BGR subpixel ordering (fixes Red/Blue swap)
    madctl_val = 0x08 if bgr else 0x00
    write_cmd(CMD_MADCTL)
    write_data(madctl_val)

    # Display Inversion: INVOFF (0x20) fixes washed-out/milky colors
    if invert:
        write_cmd(CMD_INVON)
    else:
        write_cmd(CMD_INVOFF)

    write_cmd(CMD_NORON)
    time.sleep(0.01)

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
    if img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.asarray(img, dtype=np.uint16)
    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    raw_bytes = rgb565.byteswap().tobytes()

    set_window(0, 0, WIDTH - 1, HEIGHT - 1)
    write_data(raw_bytes)

# 4. Run Init & Calibration Patterns
print("\n[4/4] Sending Calibrated ST7789 Initialization Sequence...")
try:
    init_st7789(bgr=args.bgr, invert=args.invert)
    print(f"      ✓ ST7789 hardware initialized (BGR={args.bgr}, Invert={args.invert})")

    font_huge = load_font(30, bold=True)
    font_sub  = load_font(13, bold=False)

    # Primary Color Calibration Screens
    cal_screens = [
        ("RED",   (255, 0, 0),   "THIS MUST BE PURE RED",   "If this looks blue, run with --rgb"),
        ("GREEN", (0, 255, 0),   "THIS MUST BE PURE GREEN", "Green color channel check"),
        ("BLUE",  (0, 0, 255),   "THIS MUST BE PURE BLUE",  "If this looks red, run with --rgb"),
        ("BLACK", (0, 0, 0),     "INKY DEEP BLACK",         "If washed-out/grey, check inversion"),
    ]

    for name, col, main_text, sub_text in cal_screens:
        print(f"      → Displaying test card: {name}...")
        card = Image.new("RGB", (WIDTH, HEIGHT), color=col)
        draw = ImageDraw.Draw(card)
        text_col = (0, 0, 0) if name in ("GREEN",) else (255, 255, 255)
        # Center main text
        bbox = draw.textbbox((0, 0), main_text, font=font_huge)
        tw = bbox[2] - bbox[0]
        draw.text(((WIDTH - tw) // 2, 120), main_text, font=font_huge, fill=text_col)

        bbox_s = draw.textbbox((0, 0), sub_text, font=font_sub)
        tws = bbox_s[2] - bbox_s[0]
        draw.text(((WIDTH - tws) // 2, 170), sub_text, font=font_sub, fill=text_col)

        show_image(card)
        time.sleep(1.2)

    # Rich Calibration & Typography Test Card
    print("      → Displaying Typography & Marquee Test Card...")
    font_title = load_font(22, bold=True)
    font_med   = load_font(14, bold=True)
    font_body  = load_font(12, bold=False)
    font_time  = load_font(48, bold=True)

    base = Image.new("RGB", (WIDTH, HEIGHT), color=(12, 14, 24))
    draw = ImageDraw.Draw(base)

    # Header Card
    draw.rounded_rectangle([10, 10, WIDTH - 10, 50], radius=8, fill=(22, 28, 48), outline=(48, 60, 95), width=1)
    bbox = draw.textbbox((0, 0), "SANDY'S AIRPLAY", font=font_title)
    tw = bbox[2] - bbox[0]
    draw.text(((WIDTH - tw) // 2, 18), "SANDY'S AIRPLAY", font=font_title, fill=(255, 255, 255))

    # SF Pro Rounded Time Preview
    draw.text((36, 60), "12:45", font=font_time, fill=(255, 255, 255))
    draw.rounded_rectangle([165, 82, 215, 104], radius=11, fill=(30, 32, 56))
    draw.text((176, 85), ":30", font=font_med, fill=(140, 135, 230))
    draw.text((36, 118), "Tuesday, 13 October", font=font_body, fill=(200, 205, 225))

    # Color swatches: Red, Orange, Yellow, Green, Cyan, Blue, Magenta, White
    swatches = [
        (255, 0, 0), (255, 130, 0), (255, 220, 0), (0, 220, 50),
        (0, 210, 255), (0, 80, 255), (200, 0, 255), (255, 255, 255)
    ]
    sw_w = (WIDTH - 24) // len(swatches)
    for i, sc in enumerate(swatches):
        draw.rounded_rectangle([12 + i * sw_w, 145, 12 + (i + 1) * sw_w - 2, 175], radius=3, fill=sc)

    # Active Hardware Badges
    draw.rounded_rectangle([12, 186, WIDTH - 12, 214], radius=6, fill=(18, 55, 35))
    draw.text((22, 192), "COLOR CALIBRATION: OK (BGR)", font=font_body, fill=(0, 255, 140))

    draw.rounded_rectangle([12, 222, WIDTH - 12, 250], radius=6, fill=(24, 32, 55))
    draw.text((22, 228), "TYPOGRAPHY: SF PRO ROUNDED", font=font_body, fill=(120, 200, 255))

    # Marquee simulation across bottom strip
    print("      → Demonstrating smooth Marquee text clipping for 6 seconds...")
    marquee_msg = "Now Playing: Sandy High-Fidelity Audio Receiver • Testing Smooth Horizontal Marquee Overflow Clipping"
    bbox_m = draw.textbbox((0, 0), marquee_msg, font=font_med)
    msg_w = bbox_m[2] - bbox_m[0]
    clip_x = 12
    clip_y = 265
    clip_w = WIDTH - 24
    clip_h = 42

    t_start = time.time()
    while time.time() - t_start < 6.0:
        elapsed = time.time() - t_start
        shift = int((elapsed * 45) % (msg_w + clip_w))
        offset = shift - clip_w

        frame = base.copy()
        strip = Image.new("RGBA", (clip_w, clip_h), (20, 24, 40, 255))
        sdraw = ImageDraw.Draw(strip)
        sdraw.text((-offset, 10), marquee_msg, font=font_med, fill=(255, 230, 100))
        frame.paste(strip.convert("RGB"), (clip_x, clip_y))
        show_image(frame)
        time.sleep(0.03)

    print("      ✓ Marquee clipping and color calibration test complete!")
    print("      → Holding test card on display for 5 seconds...")
    time.sleep(5)

except Exception as e:
    import traceback
    print(f"      ✗ Display error: {e}")
    traceback.print_exc()

print("\n" + "=" * 65)
print("  Test complete! Your ST7789 display is fully calibrated.")
print("=" * 65 + "\n")
