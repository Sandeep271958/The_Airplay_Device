#!/usr/bin/env python3
"""
TFT Display Controller — AirPlay / Bluetooth Audio Receiver
============================================================

Renders display frames using Pillow into an in-memory canvas,
then pushes them to the TFT hardware via a pluggable display backend.

Display Modes (cycled with SET button):
  Mode 0: Album Art + Track Info  (art top, song title/artist below, album omitted for clean layout)
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
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

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

# Colour palette (Apple Light Theme — optimal high contrast & readability on TFT)
BG_COLOR         = (248, 249, 252)     # clean, bright off-white background
TEXT_PRIMARY     = (18, 20, 28)        # deep inky black/slate for maximum sharpness
TEXT_SECONDARY   = (90, 95, 115)       # refined charcoal grey
ACCENT_COLOR     = (0, 122, 255)       # vibrant Apple AirPlay Blue
PROGRESS_BG      = (225, 228, 238)     # light grey track
PROGRESS_FG      = (0, 122, 255)       # bright iOS Blue fill
CARD_BG          = (255, 255, 255)     # pure white pill/card fill
CARD_BORDER      = (218, 224, 236)     # subtle soft border

# Visualiser bar gradient (vibrant saturated spectrum optimized for white background)
VIS_GRADIENT = [
    (0,   180, 70),     # bottom — rich emerald green
    (30,  200, 50),
    (100, 210, 20),
    (200, 190, 0),     # mid — warm amber yellow
    (240, 140, 0),     # orange
    (245, 90,  10),
    (235, 40,  30),     # top — punchy crimson red
    (220, 10,  60),
]


# =====================================================================
# FONT LOADING  (Prioritizes authentic Apple SF Rounded typography)
# =====================================================================

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")

def _load_font(size, preferred=None):
    """
    Load a font, prioritizing bundled SF Rounded fonts in assets/fonts/,
    then falling back to system sans-serif fonts.
    """
    candidates = []
    if preferred:
        candidates.append(os.path.join(FONTS_DIR, preferred))
    candidates.extend([
        os.path.join(FONTS_DIR, "SF-Compact-Rounded.ttf"),
        os.path.join(FONTS_DIR, "SF-Pro-Rounded-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ])
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _load_font_bold(size, preferred=None):
    """Load a bold font, prioritizing SF Pro Rounded Bold."""
    candidates = []
    if preferred:
        candidates.append(os.path.join(FONTS_DIR, preferred))
    candidates.extend([
        os.path.join(FONTS_DIR, "SF-Pro-Rounded-Bold.ttf"),
        os.path.join(FONTS_DIR, "SF-Compact-Rounded.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ])
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return _load_font(size, preferred)


# Pre-load refined fonts (scaled for 240px portrait display)
_scale = DISPLAY_WIDTH / 240.0

# Idle screen typography (Apple Watch / Lock screen aesthetic)
FONT_CLOCK_TIME = _load_font_bold(int(58 * _scale), preferred="SF-Pro-Rounded-Bold.ttf")
FONT_DATE       = _load_font_bold(int(16 * _scale), preferred="SF-Pro-Rounded-Bold.ttf")
FONT_STATUS     = _load_font(int(11 * _scale), preferred="SF-Compact-Rounded.ttf")

# Music screen typography (larger proportions with album name removed)
FONT_TITLE      = _load_font_bold(int(16 * _scale), preferred="SF-Pro-Rounded-Bold.ttf")
FONT_ARTIST     = _load_font(int(13 * _scale), preferred="SF-Compact-Rounded.ttf")
FONT_ALBUM      = _load_font(int(10 * _scale), preferred="SF-Compact-Rounded.ttf")


# =====================================================================
# MARQUEE TEXT ENGINE  (Smooth scrolling with boundary clipping)
# =====================================================================

class MarqueeText:
    """
    Smooth ping-pong marquee scrolling for text that exceeds visible display width.
    Pauses naturally at start and end for optimal readability.
    """
    def __init__(self, pause_start=2.0, pause_end=1.5, scroll_speed=28.0):
        self.pause_start = pause_start
        self.pause_end   = pause_end
        self.speed       = scroll_speed  # pixels per second
        self._last_text  = ""
        self._start_time = time.monotonic()

    def get_offset(self, text, text_w, visible_w):
        if text != self._last_text:
            self._last_text  = text
            self._start_time = time.monotonic()

        overflow = text_w - visible_w
        if overflow <= 0:
            return 0

        scroll_time = overflow / self.speed
        total_cycle = self.pause_start + scroll_time + self.pause_end + scroll_time

        elapsed = (time.monotonic() - self._start_time) % total_cycle

        if elapsed < self.pause_start:
            return 0
        elif elapsed < self.pause_start + scroll_time:
            return int((elapsed - self.pause_start) * self.speed)
        elif elapsed < self.pause_start + scroll_time + self.pause_end:
            return overflow
        else:
            back_elapsed = elapsed - (self.pause_start + scroll_time + self.pause_end)
            return overflow - int(back_elapsed * self.speed)


def draw_marquee_text(canvas, draw, x, y, max_w, text, font, color, marquee):
    """
    Draw text with horizontal scrolling if it overflows max_w.
    Uses Pillow RGBA sub-strip for clean clipping with zero overflow.
    """
    if not text:
        return
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    if tw <= max_w:
        # Fits: center horizontally within the allotted width
        tx = x + (max_w - tw) // 2
        draw.text((tx, y), text, font=font, fill=color)
    else:
        # Overflow: calculate offset and clip
        offset = marquee.get_offset(text, tw, max_w)
        strip_h = th + 6
        strip = Image.new("RGBA", (max_w, strip_h), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(strip)
        sdraw.text((-offset, 0), text, font=font, fill=color)
        canvas.paste(strip, (x, y), strip)


# =====================================================================
# TRACK METADATA  (thread-safe container with instant cache invalidation)
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
        self.art_img = None           # PIL Image (for general/legacy use)
        self.art_fitted_info = None   # Pre-rendered 240x240 RGB for Mode 0 (art + info)
        self.art_fitted_full = None   # Pre-rendered 240x320 RGB for Mode 1 (fullscreen)
        self._art_raw  = b""          # raw bytes for dedup
        self._art_time = 0.0          # monotonic timestamp when art was decoded
        # Real-time progress tracking
        self.prog_elapsed_base   = 0.0     # Elapsed seconds at last sync point
        self.prog_duration       = 0.0     # Total duration in seconds
        self.prog_total_duration = 0.0     # Authoritative duration from astm / MPRIS
        self.prog_update_time    = time.monotonic()  # monotonic clock at last sync point
        self.is_playing          = False

    @staticmethod
    def _fit_image(img, target_w, target_h):
        """Pre-scale and center-crop image to exact target dimensions once at ingest."""
        src_w, src_h = img.size
        scale = max(target_w / src_w, target_h / src_h)
        new_w = max(target_w, int(src_w * scale))
        new_h = max(target_h, int(src_h * scale))
        resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
        left = (new_w - target_w) // 2
        top  = (new_h - target_h) // 2
        return resized.crop((left, top, left + target_w, top + target_h))

    def on_track_change(self):
        """Immediately clear previous track metadata and progress on track skip."""
        global _last_prgr_string
        _last_prgr_string = ""
        with self._lock:
            self.title   = ""
            self.artist  = ""
            self.album   = ""
            self.art_img = None
            self.art_fitted_info = None
            self.art_fitted_full = None
            self._art_raw = b""
            self._art_time = 0.0
            self.prog_elapsed_base   = 0.0
            self.prog_duration       = 0.0
            self.prog_total_duration = 0.0
            self.prog_update_time    = time.monotonic()

    def set_field(self, key, value, keep_recent_art=False):
        with self._lock:
            if key == "title":
                if value and value != self.title:
                    global _last_prgr_string
                    _last_prgr_string = ""
                    self.title = value
                    now = time.monotonic()
                    # Invalidate old album art unless fresh art was just decoded within this track transition
                    if not (keep_recent_art and (now - self._art_time) < 3.0):
                        self.art_img = None
                        self.art_fitted_info = None
                        self.art_fitted_full = None
                        self._art_raw = b""
                    # Reset progress anchors for the fresh track
                    self.prog_elapsed_base   = 0.0
                    self.prog_duration       = 0.0
                    self.prog_total_duration = 0.0
                    self.prog_update_time    = now
            elif key == "artist":
                self.artist = value
            elif key == "album":
                self.album = value

    def set_art(self, raw_bytes):
        """Decode and pre-scale album art. Skips if identical to current."""
        if not raw_bytes:
            return
        with self._lock:
            if raw_bytes == self._art_raw and self.art_img is not None:
                return  # same image, skip the decode
            self._art_raw = raw_bytes
            try:
                decoded = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
                self.art_img = decoded
                # Pre-scale ONCE into exact target resolutions to eliminate per-frame resizing overhead
                self.art_fitted_info = self._fit_image(decoded, DISPLAY_WIDTH, DISPLAY_WIDTH)
                self.art_fitted_full = self._fit_image(decoded, DISPLAY_WIDTH, DISPLAY_HEIGHT)
                self._art_time = time.monotonic()
                print(f"[TFT-META] Decoded & pre-scaled album art ({decoded.width}x{decoded.height})")
            except Exception as e:
                print(f"[TFT-META] Art decode error: {e}")
                self.art_img = None
                self.art_fitted_info = None
                self.art_fitted_full = None

    def set_total_duration(self, duration_sec):
        """Set authoritative total song duration from astm or D-Bus mpris:length."""
        with self._lock:
            dur = float(duration_sec)
            if 1.0 <= dur <= 43200.0:
                self.prog_total_duration = dur
                if self.prog_duration <= 0.0:
                    self.prog_duration = dur
                elif self.prog_duration < (dur - 2.0):
                    # We previously received a seeked stream slice (e.g. 105s remaining of 180s total)
                    offset = dur - self.prog_duration
                    self.prog_elapsed_base += offset
                    self.prog_duration = dur
                    self.prog_update_time = time.monotonic()

    def set_progress(self, a, b=None, c=None, is_playing=None):
        """
        Update progress anchor from AirPlay prgr packet or D-Bus ProgressString.
        Supports both signatures:
          set_progress(elapsed, duration, is_playing=...)
          set_progress(start, current, end)
        """
        with self._lock:
            if c is not None:
                # Legacy: start, current, end in seconds
                duration = float(c) - float(a)
                elapsed  = float(b) - float(a)
            elif b is not None:
                elapsed  = float(a)
                duration = float(b)
            else:
                elapsed  = float(a)
                duration = 0.0

            # If authoritative total duration is known from astm or MPRIS:
            if self.prog_total_duration > 0.0:
                if duration > (self.prog_total_duration + 2.0):
                    self.prog_total_duration = duration
                total = self.prog_total_duration
                # Check if 'duration' from prgr is only the remaining duration:
                # (e.g. user started at 75s of 180s track: duration=105s, elapsed=0s)
                if duration > 0.0 and duration < (total - 2.0):
                    offset = total - duration
                    elapsed = offset + elapsed
                duration = total
            elif duration > 0.0:
                self.prog_total_duration = duration

            if duration > 0.0:
                elapsed = max(0.0, min(duration, elapsed))
            else:
                duration = max(0.0, duration)
                elapsed = max(0.0, elapsed)

            self.prog_elapsed_base = elapsed
            self.prog_duration     = duration
            self.prog_update_time  = time.monotonic()
            if is_playing is not None:
                self.is_playing    = bool(is_playing)

    def set_playing_state(self, playing):
        """Update playing state to freeze or resume real-time progress clock."""
        with self._lock:
            now = time.monotonic()
            if self.is_playing and not playing:
                # Freeze elapsed time at current monotonic offset
                dt = now - self.prog_update_time
                self.prog_elapsed_base = min(self.prog_duration, self.prog_elapsed_base + dt)
                self.prog_update_time  = now
            elif not self.is_playing and playing:
                # Resume: re-anchor clock
                self.prog_update_time  = now
            self.is_playing = bool(playing)

    def has_meta(self):
        with self._lock:
            return bool(self.title or self.artist or self.art_img)

    def snapshot(self):
        """Return an immutable copy of all fields with real-time interpolated progress."""
        with self._lock:
            now = time.monotonic()
            if self.is_playing and self.prog_duration > 0.0:
                elapsed = self.prog_elapsed_base + (now - self.prog_update_time)
                elapsed = max(0.0, min(self.prog_duration, elapsed))
            else:
                elapsed = max(0.0, min(self.prog_duration, self.prog_elapsed_base))

            duration = self.prog_duration
            frac = (elapsed / duration) if duration > 0.0 else 0.0

            return {
                "title":         self.title,
                "artist":        self.artist,
                "album":         self.album,
                "art":           self.art_img,
                "art_info":      self.art_fitted_info,
                "art_full":      self.art_fitted_full,
                "prog_start":    0.0,
                "prog_current":  elapsed,
                "prog_end":      duration,
                "prog_elapsed":  elapsed,
                "prog_duration": duration,
                "prog_frac":     frac,
                "is_playing":    self.is_playing,
            }

    def clear(self):
        global _last_prgr_string
        _last_prgr_string = ""
        with self._lock:
            self.title   = ""
            self.artist  = ""
            self.album   = ""
            self.art_img = None
            self.art_fitted_info = None
            self.art_fitted_full = None
            self._art_raw = b""
            self._art_time = 0.0
            self.prog_elapsed_base   = 0.0
            self.prog_duration       = 0.0
            self.prog_total_duration = 0.0
            self.prog_update_time    = time.monotonic()
            self.is_playing          = False


track_meta = TrackMeta()

# Buffer for chunked cover art (ssnc PICT / pcst / pcen)
_art_chunks = bytearray()
_in_picture_stream = False


_dbus_wake_event = threading.Event()

def _trigger_dbus_sync():
    """Wake the D-Bus poller immediately to sync seek position & metadata with zero delay."""
    _dbus_wake_event.set()


_last_prgr_string = ""

def _process_prgr_string(s):
    """
    Parse a 3-part progress string (start/current/end RTP timestamps).
    Handles signed deltas, uint32 rollovers, and offsets accurately.
    Deduplicates identical packets to prevent clobbering live monotonic interpolation.
    """
    global _last_prgr_string
    try:
        raw_s = str(s).strip()
        if not raw_s or "/" not in raw_s:
            return

        # Deduplication: If the packet is identical to the last one processed,
        # ignore it so we don't reset the live monotonic playback clock back to the starting anchor!
        if raw_s == _last_prgr_string:
            return
        _last_prgr_string = raw_s

        parts = raw_s.split("/")
        if len(parts) == 3:
            start_rtp = int(parts[0])
            curr_rtp  = int(parts[1])
            end_rtp   = int(parts[2])

            dur_frames = (end_rtp - start_rtp) & 0xFFFFFFFF
            raw_diff   = (curr_rtp - start_rtp) & 0xFFFFFFFF
            # Guard against buffer pre-roll jitter where curr_rtp < start_rtp
            if raw_diff > 0x7FFFFFFF:
                elapsed_frames = 0
            else:
                elapsed_frames = raw_diff

            rate = 44100.0
            dur_sec     = dur_frames / rate
            elapsed_sec = elapsed_frames / rate

            if 1.0 <= dur_sec <= 43200.0:
                elapsed_sec = max(0.0, min(dur_sec, elapsed_sec))
                # Preserve authoritative playing/paused state rather than blindly clobbering pause
                playing_now = display_controller.is_playing
                track_meta.set_progress(elapsed_sec, dur_sec, is_playing=playing_now)
                print(f"[TFT-META] Progress sync: {elapsed_sec:.1f}s / {dur_sec:.1f}s ({(elapsed_sec/dur_sec)*100:.1f}%)")
    except Exception as e:
        print(f"[TFT-META] Progress parse error: {e}")


# =====================================================================
# SHAIRPORT-SYNC METADATA PARSER  (background thread)
# =====================================================================

def metadata_reader_thread():
    """
    Read shairport-sync's metadata pipe (/tmp/shairport-sync-metadata)
    and extract track info + album art.
    """
    import stat

    # Ensure FIFO exists with 0666 permissions (so shairport-sync daemon can write)
    try:
        if os.path.exists(METADATA_PIPE):
            if not stat.S_ISFIFO(os.stat(METADATA_PIPE).st_mode):
                os.remove(METADATA_PIPE)
                os.mkfifo(METADATA_PIPE, 0o666)
        else:
            os.mkfifo(METADATA_PIPE, 0o666)
        os.chmod(METADATA_PIPE, 0o666)
    except Exception as e:
        print(f"[TFT-META] Pipe setup note: {e}")

    print(f"[TFT-META] Listening on {METADATA_PIPE}")

    re_type   = re.compile(r'<type>([0-9a-fA-F]+)</type>')
    re_code   = re.compile(r'<code>([0-9a-fA-F]+)</code>')
    re_data   = re.compile(r'<data encoding="base64">(.*?)</data>', re.DOTALL)
    re_length = re.compile(r'<length>(\d+)</length>')

    # Background D-Bus MPRIS metadata & cover art polling fallback
    def _dbus_poller():
        while True:
            # Wake on event (seek, track start, flush) or at 1.0s regular cadence
            _dbus_wake_event.wait(timeout=1.0)
            _dbus_wake_event.clear()
            try:
                if not (display_controller._connected or display_controller._playing or track_meta.has_meta()):
                    continue

                # 1. Query D-Bus MPRIS for Title, Artist, Art, and Total Duration (mpris:length)
                res = subprocess.run(
                    ["dbus-send", "--system", "--print-reply",
                     "--dest=org.mpris.MediaPlayer2.ShairportSync",
                     "/org/mpris/MediaPlayer2",
                     "org.freedesktop.DBus.Properties.Get",
                     "string:org.mpris.MediaPlayer2.Player",
                     "string:Metadata"],
                    capture_output=True, text=True, timeout=1,
                )
                if res.returncode == 0:
                    if "xesam:title" in res.stdout:
                        m_t = re.search(r'string\s+"xesam:title"\s+variant\s+string\s+"([^"]+)"', res.stdout)
                        if m_t and m_t.group(1) != track_meta.title:
                            track_meta.set_field("title", m_t.group(1), keep_recent_art=True)
                            print(f"[TFT-META] D-Bus Title: {m_t.group(1)}")
                    if "xesam:artist" in res.stdout:
                        m_a = re.search(r'string\s+"xesam:artist"\s+variant\s+array\s+\[\s+string\s+"([^"]+)"', res.stdout)
                        if m_a and m_a.group(1) != track_meta.artist:
                            track_meta.set_field("artist", m_a.group(1))
                            print(f"[TFT-META] D-Bus Artist: {m_a.group(1)}")
                    # Load authoritative art from exact mpris:artUrl if art not already streamed via pipe
                    if "mpris:artUrl" in res.stdout and track_meta.art_img is None:
                        m_u = re.search(r'string\s+"mpris:artUrl"\s+variant\s+string\s+"([^"]+)"', res.stdout)
                        if m_u:
                            url = m_u.group(1)
                            if url.startswith("file://"):
                                fpath = url[7:]
                                if os.path.exists(fpath):
                                    try:
                                        with open(fpath, "rb") as af:
                                            track_meta.set_art(af.read())
                                            print(f"[TFT-META] D-Bus loaded art from: {fpath}")
                                    except Exception:
                                        pass
                    # Authoritative Track Duration in microseconds from MPRIS
                    if "mpris:length" in res.stdout:
                        m_l = re.search(r'string\s+"mpris:length"\s+variant\s+(?:int64|uint64)\s+(\d+)', res.stdout)
                        if m_l:
                            dur_sec = int(m_l.group(1)) / 1_000_000.0
                            track_meta.set_total_duration(dur_sec)

                # 2. Synchronize PlaybackStatus / PlayerState (Paused vs Playing)
                res_status = subprocess.run(
                    ["dbus-send", "--system", "--print-reply",
                     "--dest=org.mpris.MediaPlayer2.ShairportSync",
                     "/org/mpris/MediaPlayer2",
                     "org.freedesktop.DBus.Properties.Get",
                     "string:org.mpris.MediaPlayer2.Player",
                     "string:PlaybackStatus"],
                    capture_output=True, text=True, timeout=1,
                )
                if res_status.returncode == 0:
                    if "Paused" in res_status.stdout:
                        display_controller.set_playing(False)
                    elif "Playing" in res_status.stdout:
                        display_controller.set_playing(True)
                else:
                    res_native = subprocess.run(
                        ["dbus-send", "--system", "--print-reply",
                         "--dest=org.gnome.ShairportSync",
                         "/org/gnome/ShairportSync",
                         "org.freedesktop.DBus.Properties.Get",
                         "string:org.gnome.ShairportSync.RemoteControl",
                         "string:PlayerState"],
                        capture_output=True, text=True, timeout=1,
                    )
                    if res_native.returncode == 0:
                        if "Paused" in res_native.stdout:
                            display_controller.set_playing(False)
                        elif "Playing" in res_native.stdout:
                            display_controller.set_playing(True)

                # 3. Query D-Bus ProgressString for real-time seek & position tracking
                res_prgr = subprocess.run(
                    ["dbus-send", "--system", "--print-reply",
                     "--dest=org.gnome.ShairportSync",
                     "/org/gnome/ShairportSync",
                     "org.freedesktop.DBus.Properties.Get",
                     "string:org.gnome.ShairportSync.RemoteControl",
                     "string:ProgressString"],
                    capture_output=True, text=True, timeout=1,
                )
                if res_prgr.returncode == 0:
                    m_p = re.search(r'string\s+"([^"]+)"', res_prgr.stdout)
                    if m_p and "/" in m_p.group(1):
                        _process_prgr_string(m_p.group(1))

            except Exception:
                pass

    threading.Thread(target=_dbus_poller, daemon=True, name="DBusMetaPoller").start()

    while True:
        try:
            with open(METADATA_PIPE, "r", encoding="utf-8", errors="replace") as pipe:
                buffer = ""
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        # Writer closed the pipe — re-open on next session
                        break
                    buffer += chunk
                    while "</item>" in buffer:
                        item_str, buffer = buffer.split("</item>", 1)
                        item_str += "</item>"
                        _process_metadata_item(item_str, re_type, re_code,
                                               re_data, re_length)
        except (OSError, IOError):
            time.sleep(1)


def _process_metadata_item(xml_text, re_type, re_code, re_data, re_length):
    """Parse a single metadata <item> and update track_meta."""
    global _art_chunks, _in_picture_stream
    m_code = re_code.search(xml_text)
    if not m_code:
        return

    code_hex = m_code.group(1)
    try:
        code_ascii = bytes.fromhex(code_hex).decode("ascii", errors="ignore")
    except ValueError:
        return

    m_data = re_data.search(xml_text)
    if not m_data:
        # Markers without payload data
        if code_ascii in ("pbeg", "prsm"):
            global _last_prgr_string
            _last_prgr_string = ""
            print(f"[TFT-META] AirPlay playback active ({code_ascii})")
            display_controller.set_playing(True)
            display_controller.set_connected(True)
            track_meta.set_playing_state(True)
        elif code_ascii in ("paus", "pend"):
            print(f"[TFT-META] AirPlay playback paused/ended ({code_ascii})")
            display_controller.set_playing(False)
            track_meta.set_playing_state(False)
        elif code_ascii == "pfls":
            # Stream flushed (seek / pause / track change) — discard in-flight art buffer & wake poller
            _last_prgr_string = ""
            print("[TFT-META] Stream flushed (pfls)")
            _in_picture_stream = False
            _art_chunks.clear()
            _trigger_dbus_sync()
        elif code_ascii == "pcst":
            # Picture stream start
            print("[TFT-META] Album art stream starting (pcst)...")
            _in_picture_stream = True
            _art_chunks.clear()
            # Note: preserve existing art until new image finishes decoding in pcen
        elif code_ascii in ("pcen", "pfen"):
            # Picture stream end — assemble all buffered chunks
            _in_picture_stream = False
            if _art_chunks:
                print(f"[TFT-META] Assembled complete album art ({len(_art_chunks)} bytes)")
                track_meta.set_art(bytes(_art_chunks))
                _art_chunks.clear()
                display_controller.set_connected(True)
        return

    try:
        raw_b64 = m_data.group(1).strip()
        raw_data = base64.b64decode(raw_b64)
    except Exception:
        return

    if code_ascii == "minm":
        title = raw_data.decode("utf-8", errors="replace")
        print(f"[TFT-META] Track Title: {title}")
        track_meta.set_field("title", title, keep_recent_art=True)
        display_controller.set_connected(True)
    elif code_ascii == "asar":
        artist = raw_data.decode("utf-8", errors="replace")
        print(f"[TFT-META] Artist: {artist}")
        track_meta.set_field("artist", artist)
        display_controller.set_connected(True)
    elif code_ascii == "asal":
        album = raw_data.decode("utf-8", errors="replace")
        print(f"[TFT-META] Album: {album}")
        track_meta.set_field("album", album)
        display_controller.set_connected(True)
    elif code_ascii in ("PICT", "pfmd"):
        # Picture payload data — could be a chunk in a sequence or standalone image
        _art_chunks.extend(raw_data)

        if not _in_picture_stream:
            # Standalone single-frame picture without pcst/pcen:
            # Check if current buffer can be fully loaded (pixels decoded)
            if _art_chunks.startswith(b'\xff\xd8') or _art_chunks.startswith(b'\x89PNG'):
                try:
                    test_img = Image.open(io.BytesIO(bytes(_art_chunks)))
                    test_img.load()  # Strictly verifies the complete image is present
                    print(f"[TFT-META] Received complete single-frame Cover Art ({len(_art_chunks)} bytes)")
                    track_meta.set_art(bytes(_art_chunks))
                    _art_chunks.clear()
                    display_controller.set_connected(True)
                except Exception:
                    # Partial data, wait for remaining chunks or pcen
                    pass
    elif code_ascii == "prgr":
        try:
            _process_prgr_string(raw_data.decode("ascii", errors="ignore"))
        except Exception as e:
            print(f"[TFT-META] prgr dispatch error: {e}")
    elif code_ascii == "astm":
        # Authoritative track total duration in milliseconds from DAAP metadata
        try:
            if len(raw_data) == 4:
                ms = int.from_bytes(raw_data, byteorder="big")
            else:
                ms = int(raw_data.decode("ascii", errors="ignore").strip())
            dur_sec = ms / 1000.0
            if 1.0 <= dur_sec <= 43200.0:
                print(f"[TFT-META] Song total duration (astm): {dur_sec:.1f}s")
                track_meta.set_total_duration(dur_sec)
        except Exception as e:
            print(f"[TFT-META] astm parse error: {e}")


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
    Direct hardware backend for ST7789-based TFT displays.
    Communicates directly via spidev (/dev/spidev4.0) and gpiozero (lgpio).
    Bypasses external gpiodevice/libgpiod libraries to prevent resource busy errors.
    """

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

    def __init__(self, width, height,
                 spi_port=4, spi_cs=0,
                 dc_pin=24, rst_pin=12, bl_pin=20,
                 spi_speed_hz=32_000_000,
                 bgr_mode=False, invert_color=False):
        super().__init__(width, height)
        self._spi_port     = spi_port
        self._spi_cs       = spi_cs
        self._dc_pin       = dc_pin
        self._rst_pin      = rst_pin
        self._bl_pin       = bl_pin
        self._spi_speed_hz = spi_speed_hz
        self._bgr_mode     = bgr_mode       # False = RGB mode (visually confirmed on physical LCD)
        self._invert_color = invert_color   # False uses INVOFF to fix washed-out colors

        self._spi          = None
        self._dc           = None
        self._rst          = None
        self._bl           = None
        self._ready        = False

    def _write_cmd(self, cmd):
        self._dc.off()
        self._spi.writebytes([cmd])

    def _write_data(self, data):
        self._dc.on()
        if isinstance(data, int):
            self._spi.writebytes([data])
        elif isinstance(data, (list, tuple)):
            self._spi.writebytes(list(data))
        else:
            # Transfer byte buffer in 4096-byte chunks (Linux spidev bufsiz limit)
            CHUNK = 4096
            for i in range(0, len(data), CHUNK):
                self._spi.writebytes2(data[i:i + CHUNK])

    def _set_window(self, x0, y0, x1, y1):
        self._write_cmd(self.CMD_CASET)
        self._write_data([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self._write_cmd(self.CMD_RASET)
        self._write_data([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self._write_cmd(self.CMD_RAMWR)

    def _init_hardware(self):
        """Initialise GPIO control pins, SPI4 device, and ST7789 controller registers."""
        try:
            import spidev
            from gpiozero import OutputDevice
        except ImportError as e:
            print(f"[TFT] Required libraries missing ({e}) — falling back to stub backend")
            self._ready = False
            return

        port = self._spi_port
        cs = self._spi_cs

        # Auto-detect SPI device if /dev/spidev4.0 is not present
        if not os.path.exists(f"/dev/spidev{port}.{cs}"):
            for p in [4, 1, 0]:
                if os.path.exists(f"/dev/spidev{p}.{cs}"):
                    port = p
                    break
                elif os.path.exists(f"/dev/spidev{p}.0"):
                    port = p
                    cs = 0
                    break

        if not os.path.exists(f"/dev/spidev{port}.{cs}"):
            print(f"[TFT] WARNING: Dedicated SPI device /dev/spidev{port}.{cs} not found!")
            print("      ✦ Ensure dtoverlay=spi4-1cs,cs0_pin=25 is in /boot/firmware/config.txt")
            self._ready = False
            return

        print(f"[TFT] Initialising ST7789 on /dev/spidev{port}.{cs} (DC={self._dc_pin}, RST={self._rst_pin}, BL={self._bl_pin}, BGR={self._bgr_mode}, Invert={self._invert_color})...")

        try:
            # 1. Claim GPIO lines via gpiozero (lgpio)
            self._dc = OutputDevice(self._dc_pin, active_high=True, initial_value=False)
            self._rst = OutputDevice(self._rst_pin, active_high=True, initial_value=True)
            self._bl = OutputDevice(self._bl_pin, active_high=True, initial_value=False)

            # 2. Turn backlight ON
            self._bl.on()

            # 3. Open hardware SPI bus
            self._spi = spidev.SpiDev()
            self._spi.open(port, cs)
            self._spi.max_speed_hz = self._spi_speed_hz
            self._spi.mode = 0

            # 4. Hardware reset cycle
            self._rst.on()
            time.sleep(0.05)
            self._rst.off()
            time.sleep(0.05)
            self._rst.on()
            time.sleep(0.15)

            # 5. ST7789 controller configuration sequence
            self._write_cmd(self.CMD_SWRESET)
            time.sleep(0.15)

            self._write_cmd(self.CMD_SLPOUT)
            time.sleep(0.12)

            self._write_cmd(self.CMD_COLMOD)
            self._write_data(0x55)  # 16-bit RGB565

            # MADCTL: Bit 3 controls RGB/BGR subpixel mapping (0x08 = BGR, 0x00 = RGB)
            madctl_val = 0x08 if self._bgr_mode else 0x00
            self._write_cmd(self.CMD_MADCTL)
            self._write_data(madctl_val)

            # Display Inversion: INVOFF (0x20) fixes washed-out colors on standard LCDs
            if self._invert_color:
                self._write_cmd(self.CMD_INVON)
            else:
                self._write_cmd(self.CMD_INVOFF)

            self._write_cmd(self.CMD_NORON)   # Normal display mode
            time.sleep(0.01)

            self._write_cmd(self.CMD_DISPON)  # Main display ON
            time.sleep(0.1)

            self._ready = True
            print(f"[TFT] ST7789 initialised successfully at {self._spi_speed_hz // 1_000_000} MHz (BGR={self._bgr_mode}, Invert={self._invert_color})!")
        except Exception as e:
            print(f"[TFT] ST7789 hardware init error: {e}")
            self._ready = False

    def show(self, image):
        if not self._ready:
            if self._spi is None and self._dc is None:
                self._init_hardware()
            if not self._ready:
                return

        try:
            if image.size != (self.width, self.height):
                image = image.resize((self.width, self.height))
            if image.mode != "RGB":
                image = image.convert("RGB")

            # Fast vectorised conversion to RGB565 byte buffer
            arr = np.asarray(image, dtype=np.uint16)
            r = arr[:, :, 0]
            g = arr[:, :, 1]
            b = arr[:, :, 2]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            raw_bytes = rgb565.byteswap().tobytes()

            self._set_window(0, 0, self.width - 1, self.height - 1)
            self._write_data(raw_bytes)
        except Exception as e:
            print(f"[TFT] Frame draw error: {e}")

    def cleanup(self):
        try:
            if self._bl:
                self._bl.off()
                self._bl.close()
            if self._dc:
                self._dc.close()
            if self._rst:
                self._rst.close()
            if self._spi:
                self._spi.close()
        except Exception:
            pass
        self._ready = False


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
        self.title_marquee  = MarqueeText(pause_start=2.0, pause_end=1.5, scroll_speed=26.0)
        self.artist_marquee = MarqueeText(pause_start=2.0, pause_end=1.5, scroll_speed=22.0)
        self.album_marquee  = MarqueeText(pause_start=2.5, pause_end=1.5, scroll_speed=20.0)

    # ----- Mode 0: Album Art + Track Info -----

    def render_art_info(self, meta, cava_vals):
        """
        Top portion: album art (240x240 square).
        Bottom portion (80px strip): song title and artist with marquee clipping, plus progress bar.
        """
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # Layout: art is a perfect square at the top (0..240)
        art_h = self.w
        if meta.get("art_info"):
            img.paste(meta["art_info"], (0, 0))
        elif meta.get("art"):
            art = self._fit_square(meta["art"], self.w, art_h)
            img.paste(art, (0, 0))
        else:
            self._draw_mini_vis(draw, 0, 0, self.w, art_h, cava_vals)

        # Dynamic Audio Equalizer Badge (bottom-right corner of album art)
        pill_w = int(30 * _scale)
        pill_h = int(22 * _scale)
        badge_x = self.w - pill_w - int(8 * _scale)
        badge_y = art_h - pill_h - int(8 * _scale)
        self._draw_eq_badge(img, badge_x, badge_y, meta.get("is_playing", False), cava_vals)

        # Bottom info strip (y = 240..320)
        max_w = self.w - int(20 * _scale)   # 220px max width with 10px margins
        margin_x = int(10 * _scale)
        info_y = art_h + int(8 * _scale)    # y = 248 (8px clean padding below album art)

        # Title: 16pt SF Pro Rounded Bold with marquee
        draw_marquee_text(img, draw, margin_x, info_y, max_w,
                          meta["title"] or "AirPlay Audio",
                          FONT_TITLE, TEXT_PRIMARY, self.title_marquee)

        # Artist: 13pt SF Compact Rounded with marquee
        draw_marquee_text(img, draw, margin_x, info_y + int(23 * _scale), max_w,
                          meta["artist"] or "AirPlay Receiver",
                          FONT_ARTIST, TEXT_SECONDARY, self.artist_marquee)

        # Progress bar at very bottom: 6px thick, wider 8px margins (untouched)
        bar_y = self.h - int(12 * _scale)
        bar_h = int(6 * _scale)
        bar_margin = int(8 * _scale)
        self._draw_progress_bar(draw, bar_margin, bar_y, self.w - 2 * bar_margin,
                                bar_h, meta)

        return img

    # ----- Mode 1: Full-screen Album Art -----

    def render_art_fullscreen(self, meta, cava_vals):
        """Edge-to-edge album art, no text overlay."""
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)

        if meta.get("art_full"):
            img.paste(meta["art_full"], (0, 0))
        elif meta.get("art"):
            art = self._fit_square(meta["art"], self.w, self.h)
            img.paste(art, (0, 0))
        else:
            draw = ImageDraw.Draw(img)
            vis_h = int(self.h * 0.55)
            self._draw_mini_vis(draw, 0, 0, self.w, vis_h, cava_vals)
            max_w = self.w - int(20 * _scale)
            margin_x = int(10 * _scale)
            info_y = vis_h + int(16 * _scale)
            draw_marquee_text(img, draw, margin_x, info_y, max_w,
                              meta["title"] or "AirPlay Audio",
                              FONT_TITLE, TEXT_PRIMARY, self.title_marquee)
            draw_marquee_text(img, draw, margin_x, info_y + int(24 * _scale), max_w,
                              meta["artist"] or "",
                              FONT_ARTIST, TEXT_SECONDARY, self.artist_marquee)

        # Dynamic Audio Equalizer Badge (bottom-right corner of fullscreen art)
        pill_w = int(30 * _scale)
        pill_h = int(22 * _scale)
        badge_x = self.w - pill_w - int(10 * _scale)
        badge_y = self.h - pill_h - int(12 * _scale)
        self._draw_eq_badge(img, badge_x, badge_y, meta.get("is_playing", False), cava_vals)

        return img

    # ----- Mode 2: Live Visualiser -----

    def render_visualiser(self, cava_vals):
        """Full-screen frequency bar visualiser with gradient colouring."""
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)
        draw = ImageDraw.Draw(img)

        if not cava_vals:
            cava_vals = [0] * 8
        num_bars = max(1, len(cava_vals))
        margin   = int(8 * _scale)
        gap      = int(3 * _scale)
        total_gap = gap * (num_bars - 1)
        bar_w    = max(2, (self.w - 2 * margin - total_gap) // num_bars)
        max_h    = max(10, self.h - 2 * margin)

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
        Apple StandBy / Lock Screen aesthetic using SF Pro Rounded fonts.
        Shows: Top status capsule pill, large rounded time, seconds pill,
        day & date, audio mode indicator, and Wi-Fi connection info.
        """
        img = Image.new("RGB", (self.w, self.h), BG_COLOR)
        draw = ImageDraw.Draw(img)

        now = datetime.now()

        # 1. Top Status Capsule Pill
        status_text = "AirPlay Ready" if connected else "Sandy AirPlay"
        pill_w = int(140 * _scale)
        pill_h = int(24 * _scale)
        pill_x = (self.w - pill_w) // 2
        pill_y = int(18 * _scale)
        draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
                               radius=pill_h // 2,
                               fill=CARD_BG, outline=CARD_BORDER, width=1)
        # Dot indicator
        dot_color = (0, 185, 85) if connected else (150, 155, 170)
        dot_r = int(3 * _scale)
        dot_cx = pill_x + int(14 * _scale)
        dot_cy = pill_y + pill_h // 2
        draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=dot_color)
        draw.text((pill_x + int(24 * _scale), pill_y + int(5 * _scale)),
                  status_text, font=FONT_STATUS, fill=(30, 35, 50))

        # 2. Large Time in SF Pro Rounded Bold (deep inky black for razor-sharp readability)
        time_str = now.strftime("%H:%M")
        self._draw_text_centered(draw, int(62 * _scale), time_str,
                                 FONT_CLOCK_TIME, (18, 20, 28))

        # 3. Date & Day (prominent & bold SF Pro Rounded, perfectly proportioned)
        date_str = now.strftime("%A, %d %B")
        self._draw_text_centered(draw, int(136 * _scale), date_str,
                                 FONT_DATE, (60, 68, 88))

        # 4. Soft Accent Divider Line
        div_y = int(182 * _scale)
        margin = int(45 * _scale)
        draw.line([(margin, div_y), (self.w - margin, div_y)],
                  fill=(220, 225, 238), width=1)

        # 5. Audio Mode Pill
        mode_label = f"Mode: {mode_str}"
        mode_pill_w = int(152 * _scale)
        mode_pill_h = int(24 * _scale)
        mode_pill_x = (self.w - mode_pill_w) // 2
        mode_pill_y = int(204 * _scale)
        draw.rounded_rectangle([mode_pill_x, mode_pill_y, mode_pill_x + mode_pill_w, mode_pill_y + mode_pill_h],
                               radius=mode_pill_h // 2,
                               fill=CARD_BG, outline=CARD_BORDER, width=1)
        self._draw_text_centered(draw, mode_pill_y + int(5 * _scale), mode_label,
                                 FONT_STATUS, (70, 75, 95))

        # 6. Wi-Fi SSID Pill
        ssid = self._get_wifi_ssid()
        wifi_str = f"Wi-Fi: {ssid}" if ssid else "Wi-Fi: Connected"
        wifi_pill_w = int(160 * _scale)
        wifi_pill_h = int(24 * _scale)
        wifi_pill_x = (self.w - wifi_pill_w) // 2
        wifi_pill_y = int(238 * _scale)
        draw.rounded_rectangle([wifi_pill_x, wifi_pill_y, wifi_pill_x + wifi_pill_w, wifi_pill_y + wifi_pill_h],
                               radius=wifi_pill_h // 2,
                               fill=CARD_BG, outline=CARD_BORDER, width=1)
        self._draw_text_centered(draw, wifi_pill_y + int(5 * _scale), wifi_str,
                                 FONT_STATUS, (90, 95, 115))

        # 7. Subtle Brand Footer
        self._draw_text_centered(draw, int(288 * _scale), "Sandy's AirPlay Device",
                                 _load_font(max(8, int(9 * _scale))), (155, 160, 180))

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
        """
        Draw a track progress bar with Apple-style rounded pill track,
        crisp active fill, and prominent leading scrubber head.
        """
        duration = meta.get("prog_duration", 0.0)
        frac = meta.get("prog_frac", 0.0)
        if duration <= 0.0:
            d = meta.get("prog_end", 0.0) - meta.get("prog_start", 0.0)
            if d > 0:
                duration = d
                frac = max(0.0, min(1.0, (meta.get("prog_current", 0.0) - meta.get("prog_start", 0.0)) / d))

        radius = max(2, h // 2)

        # 1. Soft-grey pill track background with subtle border
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                               fill=PROGRESS_BG, outline=(210, 216, 230), width=1)

        # 2. Active progress fill (bright iOS Blue) & Scrubber Thumb Indicator
        if duration > 0.0:
            if frac > 0.0:
                fill_w = int(w * frac)
                fill_w = max(radius * 2, min(w, fill_w))
                draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=radius,
                                       fill=PROGRESS_FG)

            # 3. Modern Apple scrubber head thumb indicator at current playback point
            dot_cx = x + int(w * frac)
            dot_cx = max(x + radius, min(x + w - radius, dot_cx))
            dot_cy = y + radius
            dot_r = radius + 2  # 5px radius (10px diameter) on 6px bar

            # Crisp white thumb with bright iOS blue rim
            draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
                         fill=(255, 255, 255), outline=PROGRESS_FG, width=1)

    def _draw_mini_vis(self, draw, x, y, w, h, cava_vals):
        """
        Draw a small embedded visualiser (used when album art is missing).
        Has a more ambient/chill aesthetic than the full-screen version.
        """
        if not cava_vals:
            cava_vals = [0] * 8
        num_bars = max(1, len(cava_vals))
        margin = int(20 * _scale)
        gap = int(4 * _scale)
        total_gap = gap * (num_bars - 1)
        bar_w = max(2, (w - 2 * margin - total_gap) // num_bars)
        max_h = max(10, h - 2 * margin)

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

    def _draw_eq_badge(self, img, x, y, is_playing, cava_vals=None):
        """
        Draw a miniature Apple/Spotify-style animated equalizer badge in the corner of album art.
        - When playing: 4 dynamic vertical sound wave bars dance to CAVA frequency spectrum + organic sine oscillation.
        - When paused: bars settle down to a flat, motionless state (2px bar), providing an instant visual playback cue.
        """
        pill_w = int(30 * _scale)
        pill_h = int(22 * _scale)
        radius = max(3, int(6 * _scale))

        badge = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(badge)

        # Frosted glass capsule background with subtle translucent border
        d.rounded_rectangle([0, 0, pill_w - 1, pill_h - 1], radius=radius,
                            fill=(10, 14, 24, 185), outline=(255, 255, 255, 55), width=1)

        now = time.monotonic()
        num_bars = 4
        bar_w = max(2, int(3 * _scale))
        gap = max(1, int(2 * _scale))
        total_w = num_bars * bar_w + (num_bars - 1) * gap
        start_x = (pill_w - total_w) // 2
        max_bar_h = int(13 * _scale)
        base_y = pill_h - int(4 * _scale)

        frequencies = [5.2, 7.1, 6.3, 8.0]
        phases = [0.0, 1.2, 2.5, 3.8]

        for i in range(num_bars):
            if not is_playing:
                # Flat and still when paused
                bh = max(2, int(2 * _scale))
            else:
                # Organic dancing equalizer wave
                osc = abs(math.sin(now * frequencies[i] + phases[i]))
                cava_factor = 0.0
                if cava_vals and len(cava_vals) > i * 2:
                    cava_factor = cava_vals[i * 2] / 255.0
                frac = max(osc * 0.7 + cava_factor * 0.3, 0.25)
                bh = max(2, min(max_bar_h, int(frac * max_bar_h)))

            bx = start_x + i * (bar_w + gap)
            by = base_y - bh
            d.rounded_rectangle([bx, by, bx + bar_w - 1, base_y], radius=1,
                                fill=(255, 255, 255, 240))

        img.paste(badge, (x, y), badge)

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

    _cached_ssid = None
    _cached_ssid_time = 0.0

    @classmethod
    def _get_wifi_ssid(cls):
        """Get the currently connected Wi-Fi SSID (Linux only), cached for 30s to avoid frame lag."""
        now = time.monotonic()
        if cls._cached_ssid is not None and (now - cls._cached_ssid_time) < 30.0:
            return cls._cached_ssid

        ssid = None
        try:
            result = subprocess.run(
                ["iwgetid", "-r"],
                capture_output=True, text=True, timeout=1,
            )
            if result.returncode == 0 and result.stdout.strip():
                ssid = result.stdout.strip()
        except Exception:
            pass

        if not ssid:
            try:
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                    capture_output=True, text=True, timeout=1,
                )
                for line in result.stdout.splitlines():
                    if line.startswith("yes:"):
                        ssid = line.split(":", 1)[1].strip() or None
                        break
            except Exception:
                pass

        cls._cached_ssid = ssid
        cls._cached_ssid_time = now
        return ssid


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

        # Center the logo vertically in the EXACT center of the screen
        lx = (width - logo_frame.size[0]) // 2
        ly = (height - logo_frame.size[1]) // 2
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

    @property
    def is_playing(self):
        with self._lock:
            return self._playing

    def set_playing(self, v):
        with self._lock:
            self._playing = bool(v)
        track_meta.set_playing_state(v)

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
            # "Sandy" in large text (centered vertically in display)
            sandy_font = _load_font_bold(int(42 * _scale))
            bbox = draw.textbbox((0, 0), "Sandy", font=sandy_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            sandy_y = (self.h - th) // 2
            draw.text(((self.w - tw) // 2, sandy_y),
                      "Sandy", font=sandy_font, fill=(255, 255, 255))
            # "Designed by" above
            designed_font = _load_font(max(8, int(9 * _scale)))
            bbox2 = draw.textbbox((0, 0), "Designed by", font=designed_font)
            tw2 = bbox2[2] - bbox2[0]
            draw.text(((self.w - tw2) // 2, sandy_y - int(16 * _scale)),
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

            try:
                # Decide what to render
                has_track = track_meta.has_meta() and connected

                if not playing and not has_track:
                    # Idle (no active session or track loaded) → clock / status
                    frame = self.renderer.render_clock(audio_mode, connected)
                    interval = interval_idle
                else:
                    # Active track session: playing OR paused
                    meta = track_meta.snapshot()
                    meta["is_playing"] = playing  # ensure synchronized playing flag for flat EQ badge on pause
                    if mode == 0:
                        frame = self.renderer.render_art_info(meta, cava_vals)
                    elif mode == 1:
                        frame = self.renderer.render_art_fullscreen(meta, cava_vals)
                    else:
                        if not playing:
                            frame = self.renderer.render_art_info(meta, [0] * len(cava_vals))
                        else:
                            frame = self.renderer.render_visualiser(cava_vals)
                    interval = interval_active if playing else 0.1

                # Push to hardware
                self.backend.show(frame)
            except Exception as e:
                print(f"[TFT] Display error: {e}")
                time.sleep(0.5)
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
display_controller = DisplayController(backend=ST7789Backend(DISPLAY_WIDTH, DISPLAY_HEIGHT, bgr_mode=False, invert_color=False))


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

