#!/usr/bin/env python3
"""
TFT Display Controller — AirPlay / Bluetooth Audio Receiver
============================================================

Renders display frames using Pillow into an in-memory canvas,
then pushes them to the TFT hardware via a pluggable display backend.

Display Modes (cycled with SET button):
  Mode 0: Album Art + Track Info  (art top, title/artist/album below)
  Mode 1: Full-screen Album Art   (edge-to-edge, no text)
  Mode 2: Live Visualiser         (animated frequency bars on TFT)

When idle (no music playing):
  → Clock / status screen (time, mode, Wi-Fi info)

When no album art available (Bluetooth or missing cover):
  → Track info text + small animated visualiser

Architecture:
  - All drawing happens via Pillow (resolution-independent).
  - The display backend is swappable: currently a no-op stub that
    can be replaced with ST7789, ILI9341, SSD1351, etc. when the
    hardware arrives.
  - Metadata is read from shairport-sync's metadata pipe.
"""

import io
import os
import re
import time
import math
import base64
import threading
import subprocess
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# =====================================================================
# CONFIGURATION
# =====================================================================

# Display resolution — change these when you know your TFT module.
# The rendering pipeline is fully resolution-independent.
DISPLAY_WIDTH  = 240
DISPLAY_HEIGHT = 320

# Shairport-sync metadata pipe (configured in shairport-sync.conf)
METADATA_PIPE = "/tmp/shairport-sync-metadata"

# Refresh rates
DISPLAY_FPS     = 15   # TFT refresh rate (software SPI is slower, 15 is safe)
CLOCK_UPDATE_S  = 1.0  # How often the clock screen redraws

# Boot splash (easter egg)
SPLASH_LOGO     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "assets", "sandy_logo.png")
SPLASH_DURATION = 3.0  # total seconds for the boot animation
SPLASH_FPS      = 30   # smooth animation during splash

# Colour palette (dark theme)
BG_COLOR        = (10, 10, 18)        # near-black with a hint of blue
TEXT_PRIMARY     = (230, 230, 235)     # off-white
TEXT_SECONDARY   = (140, 140, 160)     # muted grey-purple
ACCENT_COLOR     = (100, 80, 220)     # subtle purple accent
PROGRESS_BG      = (40, 40, 55)       # progress bar background
PROGRESS_FG      = (100, 80, 220)     # progress bar fill

# Visualiser bar gradient (same palette as the LED matrix for consistency)
VIS_GRADIENT = [
    (0,   200, 0),     # bottom — green
    (50,  230, 0),
    (120, 255, 0),
    (200, 255, 0),     # mid — yellow
    (255, 200, 0),
    (255, 120, 0),
    (255, 50,  0),
    (255, 0,   0),     # top — red
]


# =====================================================================
# FONT LOADING
# =====================================================================

def _load_font(size):
    """
    Try to load a clean sans-serif font.  Falls back to the built-in
    bitmap font if nothing is installed (ugly but functional).
    """
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    # Last resort: Pillow's built-in bitmap font (small, not scalable)
    return ImageFont.load_default()


def _load_font_bold(size):
    """Same as _load_font but tries bold variants first."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return _load_font(size)


# Pre-load fonts at various sizes (relative to display width for scaling)
_scale = DISPLAY_WIDTH / 240.0   # 1.0 at 240px, prevents massive fonts on portrait displays

FONT_TITLE    = _load_font_bold(int(18 * _scale))
FONT_ARTIST   = _load_font(int(14 * _scale))
FONT_ALBUM    = _load_font(int(12 * _scale))
FONT_CLOCK_LG = _load_font_bold(int(48 * _scale))
FONT_CLOCK_SM = _load_font(int(14 * _scale))
FONT_STATUS   = _load_font(int(12 * _scale))


# =====================================================================
# TRACK METADATA  (thread-safe container)
# =====================================================================

class TrackMeta:
    """
    Holds the currently playing track's metadata.
    Updated by the metadata reader thread, read by the display renderer.
    """

    def __init__(self):
        self._lock   = threading.Lock()
        self.title   = ""
        self.artist  = ""
        self.album   = ""
        self.art_img = None   # PIL Image or None
        self._art_raw = b""   # raw bytes for dedup
        # Progress: all in seconds
        self.progress_start   = 0.0
        self.progress_current = 0.0
        self.progress_end     = 0.0

    def set_field(self, key, value):
        with self._lock:
            if key == "title":
                self.title = value
            elif key == "artist":
                self.artist = value
            elif key == "album":
                self.album = value

    def set_art(self, raw_bytes):
        """Decode and store album art.  Skips if identical to current."""
        with self._lock:
            if raw_bytes == self._art_raw:
                return  # same image, skip the decode
            self._art_raw = raw_bytes
            try:
                self.art_img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
            except Exception:
                self.art_img = None

    def set_progress(self, start, current, end):
        with self._lock:
            self.progress_start   = start
            self.progress_current = current
            self.progress_end     = end

    def snapshot(self):
        """Return an immutable copy of all fields for rendering."""
        with self._lock:
            return {
                "title":    self.title,
                "artist":   self.artist,
                "album":    self.album,
                "art":      self.art_img.copy() if self.art_img else None,
                "prog_start":   self.progress_start,
                "prog_current": self.progress_current,
                "prog_end":     self.progress_end,
            }

    def clear(self):
        with self._lock:
            self.title   = ""
            self.artist  = ""
            self.album   = ""
            self.art_img = None
            self._art_raw = b""
            self.progress_start   = 0.0
            self.progress_current = 0.0
            self.progress_end     = 0.0


track_meta = TrackMeta()


# =====================================================================
# SHAIRPORT-SYNC METADATA PARSER  (background thread)
# =====================================================================



def metadata_reader_thread():
    """
    Read shairport-sync's metadata pipe and extract track info + album art.

    Shairport-sync metadata pipe format (XML-like, one item per line):
      <item>
        <type>HEX</type>
        <code>HEX</code>
        <length>N</length>
        <data encoding="base64">BASE64_DATA</data>
      </item>

    Key codes (in ASCII, sent as hex):
      'asal' = album name
      'asar' = artist
      'minm' = track title
      'PICT' = cover art (raw JPEG/PNG)
      'prgr' = progress info  "start/current/end" in sample-rate units
    """
    try:
        os.mkfifo(METADATA_PIPE)
    except FileExistsError:
        pass   # already created by shairport-sync — that's fine
    except OSError as e:
        print(f"[TFT-META] Cannot create metadata pipe: {e}")
        return

    print(f"[TFT-META] Listening on {METADATA_PIPE}")

    # Regex to extract fields from the XML-ish metadata format
    re_type   = re.compile(r'<type>([0-9a-fA-F]+)</type>')
    re_code   = re.compile(r'<code>([0-9a-fA-F]+)</code>')
    re_data   = re.compile(r'<data encoding="base64">(.*?)</data>', re.DOTALL)
    re_length = re.compile(r'<length>(\d+)</length>')

    while True:
        try:
            with open(METADATA_PIPE, "r") as pipe:
                buffer = ""
                for line in pipe:
                    buffer += line
                    # Process when we see a complete </item>
                    if "</item>" in buffer:
                        _process_metadata_item(buffer, re_type, re_code,
                                               re_data, re_length)
                        buffer = ""
        except (OSError, IOError):
            time.sleep(1)


def _process_metadata_item(xml_text, re_type, re_code, re_data, re_length):
    """Parse a single metadata <item> and update track_meta."""
    m_code = re_code.search(xml_text)
    if not m_code:
        return

    code_hex = m_code.group(1)
    # Decode the 4-byte hex code to ASCII (e.g. "6d696e6d" → "minm")
    try:
        code_ascii = bytes.fromhex(code_hex).decode("ascii", errors="ignore")
    except ValueError:
        return

    # Extract base64 data (if present)
    m_data = re_data.search(xml_text)
    if not m_data:
        return
    raw_data = base64.b64decode(m_data.group(1))

    if code_ascii == "minm":
        track_meta.set_field("title", raw_data.decode("utf-8", errors="replace"))
    elif code_ascii == "asar":
        track_meta.set_field("artist", raw_data.decode("utf-8", errors="replace"))
    elif code_ascii == "asal":
        track_meta.set_field("album", raw_data.decode("utf-8", errors="replace"))
    elif code_ascii == "PICT":
        track_meta.set_art(raw_data)
    elif code_ascii == "prgr":
        # Format: "start/current/end" — values are in 44100 samples/sec
        try:
            parts = raw_data.decode("ascii").split("/")
            if len(parts) == 3:
                rate = 44100.0
                track_meta.set_progress(
                    float(parts[0]) / rate,
                    float(parts[1]) / rate,
                    float(parts[2]) / rate,
                )
        except (ValueError, IndexError):
            pass


# =====================================================================
# DISPLAY BACKEND  (pluggable — swap this when hardware arrives)
# =====================================================================

class DisplayBackend:
    """
    Abstract display backend.  Subclass this for your specific TFT.

    The display controller calls `show(pil_image)` at ~15 FPS with a
    Pillow RGB Image sized (DISPLAY_WIDTH × DISPLAY_HEIGHT).
    """

    def __init__(self, width, height):
        self.width  = width
        self.height = height

    def show(self, image):
        """Push a PIL Image to the physical display."""
        raise NotImplementedError

    def clear(self):
        """Clear the display to black."""
        self.show(Image.new("RGB", (self.width, self.height), (0, 0, 0)))

    def cleanup(self):
        """Release hardware resources."""
        pass


class StubBackend(DisplayBackend):
    """
    No-op backend for development/testing without a physical TFT.
    Optionally saves frames to disk for visual debugging.
    """

    def __init__(self, width, height, save_frames=False):
        super().__init__(width, height)
        self._save = save_frames
        self._frame = 0

    def show(self, image):
        if self._save:
            self._frame += 1
            if self._frame % 30 == 0:  # save every 30th frame to avoid spam
                image.save(f"/tmp/tft_frame_{self._frame:06d}.png")


class ST7789Backend(DisplayBackend):
    """
    Hardware backend for ST7789-based TFT displays.

    Requires the `st7789` Python package:
        pip install st7789

    Pin configuration will be set when the hardware module is purchased.
    This is a template — adjust the constructor pins accordingly.
    """

    def __init__(self, width, height,
                 spi_port=1, spi_cs=0,
                 dc_pin=24, rst_pin=12, bl_pin=20):
        super().__init__(width, height)
        self._display = None
        # Store pin config for when we activate it
        self._spi_port = spi_port
        self._spi_cs   = spi_cs
        self._dc_pin   = dc_pin
        self._rst_pin  = rst_pin
        self._bl_pin   = bl_pin

    def _init_hardware(self):
        """Lazy-init the ST7789 driver.  Called on first show()."""
        try:
            import st7789 as st7789_lib
            self._display = st7789_lib.ST7789(
                port=self._spi_port or 0,
                cs=self._spi_cs or 0,
                dc=self._dc_pin or 24,
                rst=self._rst_pin or 25,
                backlight=self._bl_pin or 13,
                width=self.width,
                height=self.height,
                rotation=0,
                spi_speed_hz=40_000_000,
            )
        except ImportError:
            print("[TFT] st7789 library not installed — falling back to stub")
            self._display = "STUB"
        except Exception as e:
            print(f"[TFT] ST7789 init failed: {e}")
            self._display = "STUB"

    def show(self, image):
        if self._display is None:
            self._init_hardware()
        if self._display == "STUB":
            return
        self._display.display(image)

    def cleanup(self):
        if self._display and self._display != "STUB":
            try:
                self._display.set_backlight(False)
            except Exception:
                pass


# =====================================================================
# DISPLAY RENDERER  (creates Pillow frames)
# =====================================================================

class DisplayRenderer:
    """
    Produces Pillow Image frames for the TFT display.

    Modes:
      0 — Album Art + Track Info
      1 — Full-screen Album Art
      2 — Live Visualiser (frequency bars)

    When idle:
      → Clock / Status screen

    When no album art (BT mode or missing cover):
      → Track info + mini visualiser
    """

    def __init__(self, width, height):
        self.w = width
        self.h = height
        self._vis_step = 0.0   # animation phase for visualiser

    # ----- Mode 0: Album Art + Track Info -----

    def render_art_info(self, meta, cava_vals):
        """
        Top portion: album art (square, centered).
        Bottom portion: title, artist, album, and a progress bar.
        """
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # Layout: art is a perfect square at the top, info fills the remaining bottom strip
        art_h = self.w
        info_y = art_h + int(8 * _scale)

        if meta["art"]:
            # Resize art to fill width, cropping to square if needed
            art = self._fit_square(meta["art"], self.w, art_h)
            img.paste(art, (0, 0))
        else:
            # No art → show mini visualiser in the art area
            self._draw_mini_vis(draw, 0, 0, self.w, art_h, cava_vals)

        # Track info
        self._draw_text_centered(draw, info_y, meta["title"] or "Unknown Title",
                                 FONT_TITLE, TEXT_PRIMARY)
        self._draw_text_centered(draw, info_y + int(22 * _scale),
                                 meta["artist"] or "Unknown Artist",
                                 FONT_ARTIST, TEXT_SECONDARY)
        self._draw_text_centered(draw, info_y + int(40 * _scale),
                                 meta["album"] or "",
                                 FONT_ALBUM, TEXT_SECONDARY)

        # Progress bar
        bar_y = self.h - int(14 * _scale)
        bar_h = int(4 * _scale)
        margin = int(12 * _scale)
        self._draw_progress_bar(draw, margin, bar_y, self.w - 2 * margin,
                                bar_h, meta)

        return img

    # ----- Mode 1: Full-screen Album Art -----

    def render_art_fullscreen(self, meta, cava_vals):
        """Edge-to-edge album art, no text overlay."""
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)

        if meta["art"]:
            art = self._fit_square(meta["art"], self.w, self.h)
            img.paste(art, (0, 0))
        else:
            # Fallback: info + mini vis (same as Mode 0 without art)
            draw = ImageDraw.Draw(img)
            vis_h = int(self.h * 0.5)
            self._draw_mini_vis(draw, 0, 0, self.w, vis_h, cava_vals)
            info_y = vis_h + int(20 * _scale)
            self._draw_text_centered(draw, info_y, meta["title"] or "No Track",
                                     FONT_TITLE, TEXT_PRIMARY)
            self._draw_text_centered(draw, info_y + int(24 * _scale),
                                     meta["artist"] or "",
                                     FONT_ARTIST, TEXT_SECONDARY)

        return img

    # ----- Mode 2: Live Visualiser -----

    def render_visualiser(self, cava_vals):
        """Full-screen frequency bar visualiser with gradient colouring."""
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)
        draw = ImageDraw.Draw(img)

        num_bars = len(cava_vals)
        margin   = int(8 * _scale)
        gap      = int(3 * _scale)
        total_gap = gap * (num_bars - 1)
        bar_w    = (self.w - 2 * margin - total_gap) // num_bars
        max_h    = self.h - 2 * margin

        for i, val in enumerate(cava_vals):
            bar_h = int((val / 255.0) * max_h)
            if bar_h < 2:
                bar_h = 2   # always show a sliver so bars are visible

            x = margin + i * (bar_w + gap)
            y_top = self.h - margin - bar_h
            y_bot = self.h - margin

            # Draw the bar with a vertical gradient
            segments = 16
            seg_h = max(1, bar_h // segments)
            for s in range(segments):
                seg_y = y_bot - (s + 1) * seg_h
                if seg_y < y_top:
                    seg_y = y_top
                frac = s / (segments - 1) if segments > 1 else 0
                color = self._gradient_color(frac)
                draw.rectangle([x, seg_y, x + bar_w, seg_y + seg_h], fill=color)
                if seg_y <= y_top:
                    break

        return img

    # ----- Idle: Clock / Status Screen -----

    def render_clock(self, mode_str, connected):
        """
        Elegant clock / status screen for idle state.
        Shows: large time, date, connection mode, Wi-Fi info.
        """
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)
        draw = ImageDraw.Draw(img)

        now = datetime.now()

        # Time — large, centered
        time_str = now.strftime("%H:%M")
        self._draw_text_centered(draw, int(self.h * 0.25), time_str,
                                 FONT_CLOCK_LG, TEXT_PRIMARY)

        # Seconds — smaller, below time
        sec_str = now.strftime(":%S")
        self._draw_text_centered(draw, int(self.h * 0.25) + int(52 * _scale),
                                 sec_str, FONT_CLOCK_SM, TEXT_SECONDARY)

        # Date
        date_str = now.strftime("%A, %d %B")
        self._draw_text_centered(draw, int(self.h * 0.60), date_str,
                                 FONT_ARTIST, TEXT_SECONDARY)

        # Divider line
        div_y = int(self.h * 0.72)
        margin = int(40 * _scale)
        draw.line([(margin, div_y), (self.w - margin, div_y)],
                  fill=ACCENT_COLOR, width=1)

        # Mode status
        status = f"{'●' if connected else '○'}  {mode_str}"
        self._draw_text_centered(draw, int(self.h * 0.78), status,
                                 FONT_STATUS, ACCENT_COLOR)

        # Wi-Fi SSID
        ssid = self._get_wifi_ssid()
        if ssid:
            wifi_str = f"Wi-Fi: {ssid}"
            self._draw_text_centered(draw, int(self.h * 0.88), wifi_str,
                                     FONT_STATUS, TEXT_SECONDARY)

        return img

    # ----- Drawing Helpers -----

    def _draw_text_centered(self, draw, y, text, font, color):
        """Draw text horizontally centered at vertical position y."""
        if not text:
            return
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        x = (self.w - tw) // 2
        draw.text((x, y), text, font=font, fill=color)

    def _draw_progress_bar(self, draw, x, y, w, h, meta):
        """Draw a track progress bar."""
        duration = meta["prog_end"] - meta["prog_start"]
        if duration <= 0:
            return
        elapsed = meta["prog_current"] - meta["prog_start"]
        frac = max(0.0, min(1.0, elapsed / duration))

        # Background
        draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2,
                               fill=PROGRESS_BG)
        # Fill
        fill_w = int(w * frac)
        if fill_w > 0:
            draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=h // 2,
                                   fill=PROGRESS_FG)

    def _draw_mini_vis(self, draw, x, y, w, h, cava_vals):
        """
        Draw a small embedded visualiser (used when album art is missing).
        Has a more ambient/chill aesthetic than the full-screen version.
        """
        num_bars = len(cava_vals)
        margin = int(20 * _scale)
        gap = int(4 * _scale)
        total_gap = gap * (num_bars - 1)
        bar_w = (w - 2 * margin - total_gap) // num_bars
        max_h = h - 2 * margin

        for i, val in enumerate(cava_vals):
            bar_h = max(3, int((val / 255.0) * max_h))
            bx = x + margin + i * (bar_w + gap)
            by = y + h - margin - bar_h

            frac = val / 255.0
            color = self._gradient_color(frac * 0.6)  # more muted
            # Rounded appearance: draw with a small radius
            draw.rounded_rectangle(
                [bx, by, bx + bar_w, y + h - margin],
                radius=max(1, bar_w // 4),
                fill=color,
            )

    def _fit_square(self, art_img, target_w, target_h):
        """Resize and center-crop an image to fit exactly target_w × target_h."""
        img = art_img.copy()
        # Scale so the smallest dimension fills the target
        src_w, src_h = img.size
        scale = max(target_w / src_w, target_h / src_h)
        new_w = int(src_w * scale)
        new_h = int(src_h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        # Center crop
        left = (new_w - target_w) // 2
        top  = (new_h - target_h) // 2
        img = img.crop((left, top, left + target_w, top + target_h))
        return img

    def _gradient_color(self, frac):
        """Map a 0.0–1.0 fraction to a colour from VIS_GRADIENT."""
        frac = max(0.0, min(1.0, frac))
        idx = frac * (len(VIS_GRADIENT) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(VIS_GRADIENT) - 1)
        t = idx - lo
        r = int(VIS_GRADIENT[lo][0] * (1 - t) + VIS_GRADIENT[hi][0] * t)
        g = int(VIS_GRADIENT[lo][1] * (1 - t) + VIS_GRADIENT[hi][1] * t)
        b = int(VIS_GRADIENT[lo][2] * (1 - t) + VIS_GRADIENT[hi][2] * t)
        return (r, g, b)

    @staticmethod
    def _get_wifi_ssid():
        """Get the currently connected Wi-Fi SSID (Linux only)."""
        try:
            result = subprocess.run(
                ["iwgetid", "-r"],
                capture_output=True, text=True, timeout=2,
            )
            return result.stdout.strip() or None
        except Exception:
            return None


# =====================================================================
# BOOT SPLASH ANIMATION  (easter egg)
# =====================================================================

def _load_splash_logo(width, height):
    """
    Load and prepare the Sandy logo for the splash screen.

    The logo PNG is white-on-light-background.  We extract just the
    white lettering as a mask and composite it onto a black background
    so it looks clean on the TFT.
    """
    if not os.path.exists(SPLASH_LOGO):
        print(f"[TFT] Splash logo not found at {SPLASH_LOGO}")
        return None

    try:
        logo = Image.open(SPLASH_LOGO).convert("RGBA")

        # Composite onto a black background to flatten alpha
        black_bg = Image.new("RGBA", logo.size, (0, 0, 0, 255))
        composite = Image.alpha_composite(black_bg, logo).convert("RGB")

        # ---- Vectorised pixel processing via numpy (fast on Pi Zero 2W) ----
        arr = np.array(composite, dtype=np.int16)  # int16 to avoid overflow
        bg_color = arr[2, 2]  # sample top-left corner for background color

        # Manhattan distance from the background color for each pixel
        dist = np.abs(arr - bg_color).sum(axis=2)
        threshold = 30

        # Luminance per pixel (mean of R, G, B)
        lum = arr.mean(axis=2)

        # Mask: pixels that ARE the background
        mask_bg = dist < threshold
        # Mask: non-background pixels that are bright (white fill of the logo)
        mask_bright = (~mask_bg) & (lum > 200)
        # Mask: non-background pixels that are darker (outlines)
        mask_outline = (~mask_bg) & (~mask_bright)

        # Apply: background → black, bright → white, outlines → brightened grey
        arr[mask_bg] = [0, 0, 0]
        arr[mask_bright] = [255, 255, 255]
        outline_lum = np.clip(lum[mask_outline].astype(np.int16) + 60, 0, 255)
        arr[mask_outline, 0] = outline_lum
        arr[mask_outline, 1] = outline_lum
        arr[mask_outline, 2] = outline_lum

        rgb = Image.fromarray(arr.astype(np.uint8), "RGB")

        # ---- Scale to fit display with padding ----
        pad = int(width * 0.1)
        target_w = width - 2 * pad
        aspect = logo.size[0] / logo.size[1]
        target_h = int(target_w / aspect)

        # Don't let it exceed 40% of display height (leave room for text)
        max_h = int(height * 0.35)
        if target_h > max_h:
            target_h = max_h
            target_w = int(target_h * aspect)

        rgb = rgb.resize((target_w, target_h), Image.LANCZOS)
        return rgb

    except Exception as e:
        print(f"[TFT] Error loading splash logo: {e}")
        return None


def render_splash_frame(width, height, logo_img, progress, glow_phase):
    """
    Render a single frame of the boot splash animation.

    Args:
        width, height: display dimensions
        logo_img: pre-processed PIL Image of the Sandy logo (white on black)
        progress: 0.0 → 1.0 over the full SPLASH_DURATION
        glow_phase: current glow oscillation value (for breathing effect)

    Animation timeline (3 seconds total):
        0.0 – 0.33  : Fade in (logo + text emerge from black)
        0.33 – 0.83 : Hold with subtle glow pulse
        0.83 – 1.0  : Fade out to black
    """
    frame = Image.new("RGB", (width, height), (0, 0, 0))

    # Calculate opacity based on timeline phase
    if progress < 0.33:
        # Phase 1: Fade in  (0% → 100% over first second)
        alpha = progress / 0.33
        # Ease-out curve for a more polished feel
        alpha = 1.0 - (1.0 - alpha) ** 2.5
    elif progress < 0.83:
        # Phase 2: Hold with subtle glow
        alpha = 1.0
    else:
        # Phase 3: Fade out (100% → 0%)
        fade_progress = (progress - 0.83) / 0.17
        alpha = 1.0 - fade_progress
        # Ease-in curve
        alpha = alpha ** 2.0

    alpha = max(0.0, min(1.0, alpha))

    # Glow effect: subtle brightness oscillation during hold phase
    if 0.33 <= progress <= 0.83:
        glow = 0.85 + 0.15 * math.sin(glow_phase)
    else:
        glow = 1.0

    brightness = alpha * glow

    if logo_img and brightness > 0.01:
        # Apply brightness to the logo
        logo_frame = logo_img.copy()
        # Multiply all pixel values by brightness
        enhancer = ImageEnhance.Brightness(logo_frame)
        logo_frame = enhancer.enhance(brightness)

        # Center the logo vertically (slightly above center to leave room for text below)
        lx = (width - logo_frame.size[0]) // 2
        ly = int(height * 0.30) - logo_frame.size[1] // 2
        frame.paste(logo_frame, (lx, ly))

        # "Designed by" text — tiny, just above the logo
        draw = ImageDraw.Draw(frame)
        designed_font = _load_font(max(8, int(9 * _scale)))
        text = "Designed by"
        bbox = draw.textbbox((0, 0), text, font=designed_font)
        tw = bbox[2] - bbox[0]
        tx = (width - tw) // 2
        ty = ly - int(16 * _scale)

        # Apply same brightness to text
        text_brightness = int(180 * brightness)
        draw.text((tx, ty), text, font=designed_font,
                  fill=(text_brightness, text_brightness, text_brightness))

    return frame


# =====================================================================
# DISPLAY CONTROLLER  (ties everything together)
# =====================================================================

class DisplayController:
    """
    Manages the display lifecycle.  Call `run()` in a background thread.

    External interface (called from main.py):
      - set_mode(mode_int)     — cycle display mode (0, 1, 2)
      - set_playing(bool)      — playing vs idle
      - set_connected(bool)    — connected vs disconnected
      - set_audio_mode(str)    — "AIRPLAY" or "BLUETOOTH"
      - set_cava_vals(list)    — latest 8 frequency values from CAVA
      - play_boot_splash()     — play the 3s boot animation (blocking)
    """

    def __init__(self, backend=None):
        self.w = DISPLAY_WIDTH
        self.h = DISPLAY_HEIGHT

        self.backend  = backend or StubBackend(self.w, self.h)
        self.renderer = DisplayRenderer(self.w, self.h)

        # State (thread-safe via a lock)
        self._lock       = threading.Lock()
        self._mode       = 0         # 0=art+info, 1=fullscreen, 2=visualiser
        self._playing    = False
        self._connected  = False
        self._audio_mode = "AIRPLAY"
        self._cava_vals  = [0] * 8
        self._running    = True

    # --- State setters (called from main.py threads) ---

    def set_mode(self, mode):
        with self._lock:
            self._mode = mode % 3

    def cycle_mode(self):
        with self._lock:
            self._mode = (self._mode + 1) % 3
            return self._mode

    def set_playing(self, v):
        with self._lock:
            self._playing = v

    def set_connected(self, v):
        with self._lock:
            self._connected = v

    def set_audio_mode(self, v):
        with self._lock:
            self._audio_mode = v

    def set_cava_vals(self, vals):
        with self._lock:
            self._cava_vals = list(vals)

    def stop(self):
        self._running = False

    # --- Boot splash (blocking, call before entering main loop) ---

    def play_boot_splash(self):
        """
        Play the 3-second boot splash animation.
        Blocking — call this once at startup before starting the render loop.
        """
        print("[TFT] Playing boot splash...")
        logo = _load_splash_logo(self.w, self.h)

        if logo is None:
            # No logo file — show a simple text-only splash instead
            frame = Image.new("RGB", (self.w, self.h), (0, 0, 0))
            draw = ImageDraw.Draw(frame)
            # "Sandy" in large text
            sandy_font = _load_font_bold(int(42 * _scale))
            bbox = draw.textbbox((0, 0), "Sandy", font=sandy_font)
            tw = bbox[2] - bbox[0]
            draw.text(((self.w - tw) // 2, int(self.h * 0.35)),
                      "Sandy", font=sandy_font, fill=(255, 255, 255))
            # "Designed by" above
            designed_font = _load_font(max(8, int(9 * _scale)))
            bbox2 = draw.textbbox((0, 0), "Designed by", font=designed_font)
            tw2 = bbox2[2] - bbox2[0]
            draw.text(((self.w - tw2) // 2, int(self.h * 0.35) - int(16 * _scale)),
                      "Designed by", font=designed_font, fill=(180, 180, 180))
            self.backend.show(frame)
            time.sleep(SPLASH_DURATION)
            self.backend.clear()
            print("[TFT] Boot splash complete (text fallback)")
            return

        interval = 1.0 / SPLASH_FPS
        total_frames = int(SPLASH_DURATION * SPLASH_FPS)
        glow_phase = 0.0

        for i in range(total_frames):
            t0 = time.monotonic()
            progress = i / total_frames
            frame = render_splash_frame(self.w, self.h, logo, progress, glow_phase)
            try:
                self.backend.show(frame)
            except Exception:
                pass
            glow_phase += 0.15  # glow breathing speed
            elapsed = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

        # Final: clear to black
        self.backend.clear()
        print("[TFT] Boot splash complete")

    # --- Main display loop ---

    def run(self):
        """
        Blocking render loop — run this in a daemon thread.
        Renders at DISPLAY_FPS when playing, slower when idle.
        """
        interval_active = 1.0 / DISPLAY_FPS
        interval_idle   = CLOCK_UPDATE_S

        print(f"[TFT] Display loop started ({self.w}×{self.h} @ {DISPLAY_FPS} FPS)")

        while self._running:
            t0 = time.monotonic()

            # Snapshot state
            with self._lock:
                mode       = self._mode
                playing    = self._playing
                connected  = self._connected
                audio_mode = self._audio_mode
                cava_vals  = list(self._cava_vals)

            # Decide what to render
            if not playing:
                # Idle → clock / status
                frame = self.renderer.render_clock(audio_mode, connected)
                interval = interval_idle
            else:
                meta = track_meta.snapshot()
                if mode == 0:
                    frame = self.renderer.render_art_info(meta, cava_vals)
                elif mode == 1:
                    frame = self.renderer.render_art_fullscreen(meta, cava_vals)
                else:
                    frame = self.renderer.render_visualiser(cava_vals)
                interval = interval_active

            # Push to hardware
            try:
                self.backend.show(frame)
            except Exception as e:
                print(f"[TFT] Display error: {e}")
                time.sleep(1)
                continue

            elapsed = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

        # Cleanup
        try:
            self.backend.clear()
            self.backend.cleanup()
        except Exception:
            pass
        print("[TFT] Display loop stopped")


# =====================================================================
# MODULE-LEVEL CONVENIENCE  (for importing from main.py)
# =====================================================================

# Singleton instances — created when this module is imported
display_controller = DisplayController(backend=ST7789Backend(DISPLAY_WIDTH, DISPLAY_HEIGHT))


def start_display_threads():
    """
    Start the metadata reader and display render threads.
    Plays the boot splash animation first (blocking for 3s).
    """
    # Play the boot splash before anything else
    display_controller.play_boot_splash()

    # Then start the normal display and metadata threads
    threading.Thread(
        target=metadata_reader_thread, daemon=True, name="MetadataReader"
    ).start()

    threading.Thread(
        target=display_controller.run, daemon=True, name="DisplayLoop"
    ).start()

    print("[TFT] Display and metadata threads started")

