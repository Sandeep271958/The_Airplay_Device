#!/usr/bin/env python3
"""
AirPlay / Bluetooth Audio Receiver & Visualizer Controller
===========================================================
Raspberry Pi (auto-detected at runtime)

Hardware:
  - PCM5102A I2S DAC          → audio output to 2.1 speakers
  - WS2812B Status LED        → connection/playback state indicator
  - WS2812B 8×8 LED Matrix    → real-time frequency visualizer
  - 5D Rocker Joystick        → media & mode controls (7 digital GPIOs)
  - TFT LCD                   → album art / visualizer display

Software Stack:
  - shairport-sync   : AirPlay 2 receiver
  - bluealsa          : Bluetooth A2DP audio sink
  - CAVA              : real-time audio frequency analysis
  - alsaloop           : relays ALSA loopback → DAC
  - rpi_ws281x        : WS2812 LED driver (SPI mode, avoids I2S conflict)

Audio Pipeline:
  shairport-sync (or bluealsa-aplay)
       ↓ writes to
  hw:Loopback,0,0  (ALSA loopback playback)
       ↕ kernel couples playback ↔ capture
  hw:Loopback,1,0  (ALSA loopback capture)
       ↓ shared via dsnoop
       ├── CAVA         → frequency bars → LED matrix
       └── alsaloop     → softvol → hw:DAC → speakers
"""

import time
import os
import math
import threading
import subprocess
import signal
import sys

from gpiozero import Button
import rpi_ws281x as ws

# TFT Display controller (safe to import even without hardware — uses stub backend)
try:
    from tft_display import display_controller, start_display_threads, track_meta
    TFT_AVAILABLE = True
except ImportError:
    TFT_AVAILABLE = False
    print("[TFT] tft_display module not found — display disabled")


# =====================================================================
# CONFIGURATION
# =====================================================================

# --- WS2812B LEDs (Hardware SPI0 MOSI on GPIO 10 = Physical Pin 19) ---
# Daisy-chain wiring topology:
#   RPi 4B Pin 19 (GPIO 10 / SPI0 MOSI) ──→ 8×8 Matrix DIN (Pixels 0–63)
#                                                 │
#                                           8×8 Matrix DOUT
#                                                 │
#                                                 ↓
#                                           Status LED DIN (Pixel 64)
#
# Hardware layout:
#   - Pixels 0–63 : 8×8 LED Matrix (64 pixels, progressive column wiring, all bottom → top)
#   - Pixel 64    : Single WS2812B Status LED connected to Matrix DOUT
LED_COUNT        = 65                    # 64 matrix LEDs + 1 status LED
LED_PIN          = 10                    # GPIO 10 (SPI0 MOSI / Physical Pin 19)
LED_FREQ_HZ      = 800000
LED_DMA          = 10
LED_BRIGHTNESS   = 40                    # 0–255; 50% reduction (from 80 to 40) to eliminate heat buildup
LED_INVERT       = False
LED_CHANNEL      = 0
LED_STRIP_TYPE   = ws.WS2811_STRIP_GRB   # WS2812B native colour order is GRB

MATRIX_PIXELS    = 64                    # Pixels 0–63: 8×8 visualizer matrix
STATUS_LED_INDEX = 64                    # Pixel 64: Dedicated status indicator LED

# --- 5D Rocker Joystick (BCM pin numbers, active-low via internal pull-up) ---
PIN_VOL_UP      = 16    # UP     → Volume Up
PIN_VOL_DOWN    = 17    # DOWN   → Volume Down
PIN_PREV        = 22    # LEFT   → Previous Track
PIN_NEXT        = 23    # RIGHT  → Next Track
PIN_PLAY_PAUSE  = 26    # MID    → Play / Pause
PIN_VIS_MODE    = 27    # SET    → Cycle Visualizer Modes
PIN_MODE_TOGGLE = 4     # RST    → Toggle AirPlay ↔ Bluetooth (hold 1 s)

BOUNCE_TIME     = 0.08  # seconds — snappier mechanical switch debounce
HOLD_TIME       = 1.0   # seconds — long-press to trigger mode toggle

# --- Paths ---
STATE_FILE      = "/tmp/airplay_state"  # written by shairport-sync hook scripts
CAVA_FIFO       = "/tmp/cava.fifo"      # CAVA raw ASCII output pipe

# --- Render ---
TARGET_FPS      = 30


# =====================================================================
# APPLICATION STATE  (thread-safe via a lock)
# =====================================================================

class AppState:
    MODE_AIRPLAY   = "AIRPLAY"
    MODE_BLUETOOTH = "BLUETOOTH"

    def __init__(self):
        self._lock      = threading.Lock()
        self._mode      = self.MODE_AIRPLAY
        self._connected = False
        self._playing   = False
        self._vis_mode  = 0     # 0 = freq bars; future modes for TFT

    @property
    def mode(self):
        with self._lock:
            return self._mode

    @mode.setter
    def mode(self, v):
        with self._lock:
            self._mode = v

    @property
    def connected(self):
        with self._lock:
            return self._connected

    @connected.setter
    def connected(self, v):
        with self._lock:
            self._connected = v

    @property
    def playing(self):
        with self._lock:
            return self._playing

    @playing.setter
    def playing(self, v):
        with self._lock:
            self._playing = v

    @property
    def vis_mode(self):
        with self._lock:
            return self._vis_mode

    @vis_mode.setter
    def vis_mode(self, v):
        with self._lock:
            self._vis_mode = v


state = AppState()


# =====================================================================
# COLOUR HELPERS & PALETTE
# =====================================================================

C = ws.Color   # shorthand: C(red, green, blue)

OFF   = C(0, 0, 0)
WHITE = C(255, 255, 255)
CYAN  = C(0, 255, 255)


# =====================================================================
# WS2812 STRIP  (initialised at module level so failures are caught early)
# =====================================================================

strip = ws.Adafruit_NeoPixel(
    LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
    LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL, LED_STRIP_TYPE,
)

# Guards ALL strip buffer writes and strip.show() calls.
# Both the render loop and cleanup handler acquire this lock.
strip_lock = threading.Lock()

try:
    strip.begin()
    strip.setBrightness(LED_BRIGHTNESS)
    # Immediately clear all pixels in buffer and transmit to hardware to blank bootup noise
    for i in range(LED_COUNT):
        strip.setPixelColor(i, OFF)
    strip.show()
    print(f"[LED]  Strip initialised and blanked (SPI mode, GPIO 10 / Pin 19, {LED_COUNT} pixels: 64 Matrix + 1 Status, Brightness={LED_BRIGHTNESS})")
except Exception as e:
    print(f"[LED]  FATAL — cannot initialise/blank WS2812: {e}")
    print("       ✦ Is SPI enabled?  sudo raspi-config → Interface Options → SPI")
    print("       ✦ Running as root? sudo python3 main.py")
    print("       ✦ Is core_freq locked? Add core_freq=500 and core_freq_min=500 to /boot/firmware/config.txt")
    sys.exit(1)

# Height-graduated gradient for each visualiser bar (row 0 = bottom, 7 = top).
# Green at the base, ramping through yellow/orange to red at the peak —
# gives a much more premium look than flat single-colour bars.
BAR_GRADIENT = [
    C(0,   200, 0),    # row 0  (bottom) — green
    C(50,  230, 0),    # row 1            — bright green
    C(120, 255, 0),    # row 2            — yellow-green
    C(200, 255, 0),    # row 3            — lime
    C(255, 200, 0),    # row 4            — yellow
    C(255, 120, 0),    # row 5            — orange
    C(255, 50,  0),    # row 6            — red-orange
    C(255, 0,   0),    # row 7  (top)     — red (peak)
]


# =====================================================================
# STATUS LED  (pixel 64, connected to 8×8 Matrix DOUT)
# =====================================================================

def compute_status_colour(step: float):
    """
    Decide what colour the status LED should be right now.

    Returns (colour, next_step).
    Does NOT touch the strip — the caller writes the pixel.

    States:
      Disconnected       → slow breathing deep violet
      Connected, idle    → static white (AirPlay) / cyan (Bluetooth)
      Connected, playing → breathing white / cyan (never fully dark)
    """
    if not state.connected:
        # Disconnected: slow-breathing deep violet (#800080-ish)
        t = (math.sin(step) + 1.0) / 2.0   # 0.0 → 1.0
        v = int(130 * t)
        return C(v, 0, v), step + 0.04

    if not state.playing:
        # Connected but idle: solid colour
        c = WHITE if state.mode == AppState.MODE_AIRPLAY else CYAN
        return c, step    # step doesn't advance — no animation

    # Connected and playing: breathing (minimum brightness 40 so it never goes dark)
    t = (math.sin(step) + 1.0) / 2.0
    v = int(40 + 215 * t)
    if state.mode == AppState.MODE_AIRPLAY:
        return C(v, v, v), step + 0.07
    else:
        return C(0, v, v), step + 0.07


# =====================================================================
# 8×8 MATRIX VISUALISER  (pixels 0–63)
# =====================================================================

def write_matrix_bars(values):
    """
    Draw 8 frequency-bar columns onto pixels 0–63 of the 8×8 matrix.

    `values`  — list of 8 ints in range 0–255 from CAVA.

    Panel wiring: Progressive column layout (all columns flow bottom → top).
      Bar 1 (col 0): pixels  0–7  (bottom → top)
      Bar 2 (col 1): pixels  8–15 (bottom → top)
      Bar 3 (col 2): pixels 16–23 (bottom → top)
      ...
      Bar 8 (col 7): pixels 56–63 (bottom → top)
    """
    for col in range(8):
        height = min(8, int(values[col] / 255.0 * 8))
        for row in range(8):
            # Progressive column index for 8x8 matrix (pixels 0–63)
            idx = col * 8 + row
            strip.setPixelColor(
                idx,
                BAR_GRADIENT[row] if row < height else OFF,
            )


def clear_matrix_bars():
    """Turn off all 64 matrix pixels (when paused, idle, or disconnected to prevent heat)."""
    for i in range(MATRIX_PIXELS):
        strip.setPixelColor(i, OFF)


# =====================================================================
# CAVA DATA READER  (background thread)
# =====================================================================

# Shared buffer holding the latest 8 frequency-band values from CAVA.
_cava_vals = [0] * 8
_cava_lock = threading.Lock()


def cava_reader_thread():
    """
    Continuously read CAVA's raw ASCII FIFO and update _cava_vals.

    CAVA writes lines like "23;180;255;90;40;12;5;0\n" at ~30 fps.
    If CAVA isn't running or the FIFO breaks, we retry after a short pause.
    """
    if not os.path.exists(CAVA_FIFO):
        try:
            os.mkfifo(CAVA_FIFO)
        except OSError as e:
            print(f"[CAVA] Cannot create FIFO {CAVA_FIFO}: {e}")
            return

    print(f"[CAVA] Listening on {CAVA_FIFO}")

    while True:
        try:
            with open(CAVA_FIFO, "r") as f:
                for line in f:
                    parts = line.strip().split(";")
                    nums = []
                    for p in parts:
                        p = p.strip()
                        if p.isdigit():
                            nums.append(min(255, int(p)))
                    if len(nums) >= 8:
                        with _cava_lock:
                            _cava_vals[:] = nums[:8]
        except (OSError, IOError):
            # FIFO was broken (CAVA restarted, etc.) — try again shortly
            time.sleep(1)
        else:
            time.sleep(0.5)


# =====================================================================
# UNIFIED RENDER LOOP  (~30 FPS)
# =====================================================================

def render_loop():
    """
    Single loop that updates the 8×8 matrix AND the status LED, then calls
    strip.show() exactly once per frame.

    Hardware daisy-chain layout:
      - Pixels 0–63 : 8×8 Matrix Visualiser (active when playing, blanked when idle/paused)
      - Pixel 64    : Status LED (attached to 8×8 Matrix DOUT)
    """
    step = 0.0
    interval = 1.0 / TARGET_FPS
    err_count = 0

    while True:
        t0 = time.monotonic()

        try:
            with strip_lock:
                # 1. 8×8 Matrix Visualiser (pixels 0–63)
                # Only light the matrix when actively playing audio — saves power and prevents heat buildup!
                if state.playing:
                    with _cava_lock:
                        vals = list(_cava_vals)
                    write_matrix_bars(vals)
                else:
                    clear_matrix_bars()
                    vals = [0] * 8

                # 2. Status LED (pixel 64, connected to 8×8 matrix DOUT)
                colour, step = compute_status_colour(step)
                strip.setPixelColor(STATUS_LED_INDEX, colour)

                # 3. Push entire 65-pixel buffer to hardware in one call
                strip.show()

            # 4. Feed CAVA data to the TFT display controller
            if TFT_AVAILABLE:
                display_controller.set_cava_vals(vals)

            err_count = 0
        except Exception as e:
            err_count += 1
            if err_count <= 5 or err_count % 100 == 0:
                print(f"[LED ERROR] Render loop exception (#{err_count}): {e}")

        elapsed = time.monotonic() - t0
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)


# =====================================================================
# STATE MONITOR  (background thread)
# =====================================================================

_last_audio_active_time = 0.0

def _get_shairport_status():
    """Query Shairport Sync over D-Bus for playback / active status."""
    # 1. Try MPRIS PlaybackStatus
    try:
        res = subprocess.run(
            ["dbus-send", "--system", "--print-reply",
             "--dest=org.mpris.MediaPlayer2.ShairportSync",
             "/org/mpris/MediaPlayer2",
             "org.freedesktop.DBus.Properties.Get",
             "string:org.mpris.MediaPlayer2.Player",
             "string:PlaybackStatus"],
            capture_output=True, text=True, timeout=1,
        )
        if "Playing" in res.stdout:
            return "playing"
        elif "Paused" in res.stdout:
            return "paused"
        elif "Stopped" in res.stdout:
            return "stopped"
    except Exception:
        pass

    # 2. Try Native ShairportSync RemoteControl PlayerState
    try:
        res = subprocess.run(
            ["dbus-send", "--system", "--print-reply",
             "--dest=org.gnome.ShairportSync",
             "/org/gnome/ShairportSync",
             "org.freedesktop.DBus.Properties.Get",
             "string:org.gnome.ShairportSync.RemoteControl",
             "string:PlayerState"],
            capture_output=True, text=True, timeout=1,
        )
        if "Playing" in res.stdout:
            return "playing"
        elif "Paused" in res.stdout:
            return "paused"
        elif "Stopped" in res.stdout:
            return "stopped"
    except Exception:
        pass

    return None


def _is_shairport_connected():
    """
    Check if an AirPlay client session is currently active.
    Uses native org.gnome.ShairportSync.Active property.
    """
    try:
        res = subprocess.run(
            ["dbus-send", "--system", "--print-reply",
             "--dest=org.gnome.ShairportSync",
             "/org/gnome/ShairportSync",
             "org.freedesktop.DBus.Properties.Get",
             "string:org.gnome.ShairportSync",
             "string:Active"],
            capture_output=True, text=True, timeout=1,
        )
        if "boolean true" in res.stdout:
            return True
        elif "boolean false" in res.stdout:
            return False
    except Exception:
        pass
    return None


def state_monitor_thread():
    """
    Poll connection/playback state every 1 second.
    Uses multi-source fusion:
      1. CAVA audio energy (audio samples flowing through Loopback)
      2. Shairport Sync D-Bus status (RemoteControl PlayerState / MPRIS PlaybackStatus)
      3. Native Shairport Sync Active session flag (connection state)
      4. Track metadata activity
      5. /tmp/airplay_state hook file
      6. bluetoothctl query (for Bluetooth mode)
    """
    global _last_audio_active_time

    while True:
        try:
            if state.mode == AppState.MODE_AIRPLAY:
                # 1. Physical audio activity from CAVA
                with _cava_lock:
                    peak_audio = max(_cava_vals) if _cava_vals else 0
                now = time.monotonic()
                if peak_audio > 10:
                    _last_audio_active_time = now

                audio_flowing = (now - _last_audio_active_time) < 1.5

                # 2. Shairport Sync playback state via D-Bus
                dbus_status = _get_shairport_status()
                shairport_conn = _is_shairport_connected()

                # 3. /tmp/airplay_state file
                file_status = None
                if os.path.exists(STATE_FILE):
                    try:
                        with open(STATE_FILE, "r") as f:
                            file_status = f.read().strip()
                    except Exception:
                        pass

                # 4. Metadata activity
                meta_active = track_meta.has_meta() if TFT_AVAILABLE else False
                meta_is_playing = track_meta.is_playing if TFT_AVAILABLE else False

                # Playback resolution: explicit pause or stop strictly overrides audio decay
                if dbus_status in ("paused", "stopped") or file_status in ("paused", "stopped"):
                    is_playing = False
                elif dbus_status == "playing" or file_status == "playing":
                    is_playing = True
                elif audio_flowing:
                    is_playing = True
                elif meta_is_playing:
                    is_playing = True
                else:
                    is_playing = False

                # Connection resolution:
                # If Shairport explicitly reports Active == false or file_status is disconnected,
                # the AirPlay session has terminated — purge stale metadata and return to idle.
                if shairport_conn is False or file_status == "disconnected":
                    is_connected = False
                    is_playing = False
                    if TFT_AVAILABLE and track_meta.has_meta():
                        track_meta.clear()
                else:
                    is_connected = (
                        is_playing
                        or (shairport_conn is True)
                        or dbus_status in ("paused", "playing")
                        or file_status in ("connected", "playing", "paused")
                        or meta_active
                    )

                state.connected = is_connected
                state.playing   = is_playing

            else:  # Bluetooth mode
                result = subprocess.run(
                    ["bluetoothctl", "info"],
                    capture_output=True, text=True, timeout=3,
                )
                if "Connected: yes" in result.stdout:
                    state.connected = True
                    with _cava_lock:
                        peak = max(_cava_vals) if _cava_vals else 0
                    state.playing = (peak > 10)
                else:
                    state.connected = False
                    state.playing   = False
                    if TFT_AVAILABLE and track_meta.has_meta():
                        track_meta.clear()

        except Exception:
            pass

        # Sync state to TFT display
        if TFT_AVAILABLE:
            display_controller.set_playing(state.playing)
            display_controller.set_connected(state.connected)

        time.sleep(1.0)


# =====================================================================
# CAVA PROCESS MANAGEMENT
# =====================================================================

_cava_proc = None


def start_cava():
    """Launch CAVA as a child process using our config file."""
    global _cava_proc
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cava.conf")
    if not os.path.exists(cfg):
        print(f"[CAVA] Config not found at {cfg} — visualiser disabled")
        return
    try:
        _cava_proc = subprocess.Popen(
            ["cava", "-p", cfg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[CAVA] Started (PID {_cava_proc.pid})")
    except FileNotFoundError:
        print("[CAVA] 'cava' binary not found — install with: sudo apt install cava")


def stop_cava():
    """Gracefully terminate the CAVA child process."""
    global _cava_proc
    if _cava_proc and _cava_proc.poll() is None:
        _cava_proc.terminate()
        try:
            _cava_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _cava_proc.kill()
        print("[CAVA] Stopped")


# =====================================================================
# MODE TOGGLE  (AirPlay ↔ Bluetooth)
# =====================================================================

def toggle_mode():
    """
    Hold RST for 1 second to switch audio modes.

    Both modes feed audio into the same ALSA Loopback bus, so the
    downstream pipeline (CAVA visualiser + alsaloop → DAC) stays
    running unchanged.
    """
    if state.mode == AppState.MODE_AIRPLAY:
        print("[MODE] Switching → BLUETOOTH")
        subprocess.run(["sudo", "systemctl", "stop", "shairport-sync"], check=False)
        subprocess.run(["sudo", "bluetoothctl", "discoverable", "on"], check=False)
        subprocess.run(["sudo", "bluetoothctl", "pairable", "on"], check=False)
        # bluealsa-aplay feeds BT audio into the same Loopback bus
        subprocess.Popen(
            ["bluealsa-aplay", "-D", "hw:Loopback,0,0", "00:00:00:00:00:00"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        state.mode      = AppState.MODE_BLUETOOTH
        state.connected = False
        state.playing   = False
        if TFT_AVAILABLE:
            display_controller.set_audio_mode("BLUETOOTH")
            display_controller.set_connected(False)
            display_controller.set_playing(False)
            track_meta.clear()  # don't show stale AirPlay metadata
        print("[MODE] BLUETOOTH active — waiting for pairing…")

    else:
        print("[MODE] Switching → AIRPLAY")
        subprocess.run(["sudo", "pkill", "-f", "bluealsa-aplay"], check=False)
        subprocess.run(["sudo", "bluetoothctl", "discoverable", "off"], check=False)
        subprocess.run(["sudo", "systemctl", "start", "shairport-sync"], check=False)
        state.mode      = AppState.MODE_AIRPLAY
        state.connected = False
        state.playing   = False
        if TFT_AVAILABLE:
            display_controller.set_audio_mode("AIRPLAY")
            display_controller.set_connected(False)
            display_controller.set_playing(False)
            track_meta.clear()  # don't show stale Bluetooth metadata
        print("[MODE] AIRPLAY active — waiting for connection…")


# =====================================================================
# MEDIA CONTROL CALLBACKS  (AirPlay & Bluetooth)
# =====================================================================

def _send_shairport_command(method):
    """
    Send media command (PlayPause, Next, Previous, VolumeUp, VolumeDown) to Shairport Sync.
    Attempts primary RemoteControl interface first.
    If that returns an error or non-zero code, falls back to MPRIS MediaPlayer2.
    Crucially NEVER sends both, avoiding double-trigger toggles on iOS.
    """
    cmd_remote = [
        "dbus-send", "--system", "--type=method_call",
        "--dest=org.gnome.ShairportSync",
        "/org/gnome/ShairportSync",
        f"org.gnome.ShairportSync.RemoteControl.{method}"
    ]
    try:
        res = subprocess.run(cmd_remote, capture_output=True, timeout=1.5)
        if res.returncode == 0:
            return True
    except Exception:
        pass

    # Fallback to MPRIS
    cmd_mpris = [
        "dbus-send", "--system", "--type=method_call",
        "--dest=org.mpris.MediaPlayer2.ShairportSync",
        "/org/mpris/MediaPlayer2",
        f"org.mpris.MediaPlayer2.Player.{method}"
    ]
    try:
        res = subprocess.run(cmd_mpris, capture_output=True, timeout=1.5)
        return res.returncode == 0
    except Exception:
        pass
    return False


def _async_shairport_command(method):
    """Dispatch command to Shairport Sync in background thread without blocking GPIO callbacks."""
    threading.Thread(target=_send_shairport_command, args=(method,), daemon=True).start()


def on_vol_up():
    """Increase volume via ALSA softvol and send VolumeUp to AirPlay source."""
    print("[BTN] Volume ▲")
    def _run():
        res = subprocess.run(["amixer", "sset", "SoftMaster", "5%+"], capture_output=True, check=False)
        if res.returncode != 0:
            for card in ("Headphones", "sndrpihifiberry", "0", "1", "2"):
                res_c = subprocess.run(["amixer", "-c", card, "sset", "SoftMaster", "5%+"], capture_output=True, check=False)
                if res_c.returncode == 0:
                    break
        if state.mode == AppState.MODE_AIRPLAY:
            _send_shairport_command("VolumeUp")
    threading.Thread(target=_run, daemon=True).start()


def on_vol_down():
    """Decrease volume via ALSA softvol and send VolumeDown to AirPlay source."""
    print("[BTN] Volume ▼")
    def _run():
        res = subprocess.run(["amixer", "sset", "SoftMaster", "5%-"], capture_output=True, check=False)
        if res.returncode != 0:
            for card in ("Headphones", "sndrpihifiberry", "0", "1", "2"):
                res_c = subprocess.run(["amixer", "-c", card, "sset", "SoftMaster", "5%-"], capture_output=True, check=False)
                if res_c.returncode == 0:
                    break
        if state.mode == AppState.MODE_AIRPLAY:
            _send_shairport_command("VolumeDown")
    threading.Thread(target=_run, daemon=True).start()


def on_next():
    """Skip to next track on connected phone (AirPlay or Bluetooth)."""
    print("[BTN] Next ▶▶")
    if TFT_AVAILABLE:
        track_meta.on_track_change()
    if state.mode == AppState.MODE_AIRPLAY:
        _async_shairport_command("Next")
    elif state.mode == AppState.MODE_BLUETOOTH:
        threading.Thread(
            target=lambda: subprocess.run(["bluetoothctl", "next"], check=False),
            daemon=True
        ).start()


def on_prev():
    """Skip to previous track on connected phone (AirPlay or Bluetooth)."""
    print("[BTN] Prev ◀◀")
    if TFT_AVAILABLE:
        track_meta.on_track_change()
    if state.mode == AppState.MODE_AIRPLAY:
        _async_shairport_command("Previous")
    elif state.mode == AppState.MODE_BLUETOOTH:
        threading.Thread(
            target=lambda: subprocess.run(["bluetoothctl", "previous"], check=False),
            daemon=True
        ).start()


def on_play_pause():
    """Toggle play / pause with instantaneous optimistic UI feedback."""
    print("[BTN] Play/Pause ⏯")
    if state.mode == AppState.MODE_AIRPLAY:
        # Optimistic UI update: instant visual feedback (0ms perceived latency)
        new_playing = not state.playing
        state.playing = new_playing
        if TFT_AVAILABLE:
            display_controller.set_playing(new_playing)
        _async_shairport_command("PlayPause")
    elif state.mode == AppState.MODE_BLUETOOTH:
        if state.playing:
            state.playing = False
            if TFT_AVAILABLE:
                display_controller.set_playing(False)
            threading.Thread(
                target=lambda: subprocess.run(["bluetoothctl", "pause"], check=False),
                daemon=True
            ).start()
        else:
            state.playing = True
            if TFT_AVAILABLE:
                display_controller.set_playing(True)
            threading.Thread(
                target=lambda: subprocess.run(["bluetoothctl", "play"], check=False),
                daemon=True
            ).start()


def on_vis_mode():
    """Cycle through visualiser / display modes (0..5)."""
    new_mode = (state.vis_mode + 1) % 6
    state.vis_mode = new_mode
    names = [
        "Album Art + Info",
        "Full-screen Album Art",
        "Live Bar Visualiser",
        "Fluid Ribbon Visualiser",
        "Circular Radial Halo",
        "3D Starfield Warp Tunnel"
    ]
    print(f"[BTN] Display mode → {names[new_mode]}")
    if TFT_AVAILABLE:
        display_controller.set_mode(new_mode)


# =====================================================================
# CLEAN SHUTDOWN
# =====================================================================

_cleaning_up = False

def cleanup(sig=None, frame=None):
    """Turn off all LEDs, stop CAVA, and exit gracefully (idempotent)."""
    global _cleaning_up
    if _cleaning_up:
        return
    _cleaning_up = True

    print("\n[SHUTDOWN] Cleaning up…")
    stop_cava()
    if TFT_AVAILABLE:
        display_controller.stop()
    try:
        with strip_lock:
            for i in range(LED_COUNT):
                strip.setPixelColor(i, OFF)
            strip.show()
    except Exception:
        pass
    print("[SHUTDOWN] Goodbye.")
    os._exit(0)


# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":
    # Detect board model for banner
    try:
        _pi_model = open("/proc/device-tree/model").read().strip('\x00').strip()
    except Exception:
        _pi_model = "Raspberry Pi"

    print("=" * 58)
    print("  AirPlay / Bluetooth Audio Receiver & Visualiser")
    print(f"  {_pi_model}")
    print("=" * 58)

    # Catch SIGTERM (from systemd stop) and SIGINT (Ctrl+C)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT,  cleanup)

    # --- Initialise 5D Rocker buttons with debouncing ---
    try:
        btns = {
            "vol_up":   Button(PIN_VOL_UP,      pull_up=True, bounce_time=BOUNCE_TIME),
            "vol_down": Button(PIN_VOL_DOWN,     pull_up=True, bounce_time=BOUNCE_TIME),
            "prev":     Button(PIN_PREV,         pull_up=True, bounce_time=BOUNCE_TIME),
            "next":     Button(PIN_NEXT,         pull_up=True, bounce_time=BOUNCE_TIME),
            "play":     Button(PIN_PLAY_PAUSE,   pull_up=True, bounce_time=BOUNCE_TIME),
            "vis":      Button(PIN_VIS_MODE,     pull_up=True, bounce_time=BOUNCE_TIME),
            "mode":     Button(PIN_MODE_TOGGLE,  pull_up=True, bounce_time=BOUNCE_TIME,
                               hold_time=HOLD_TIME),
        }
    except Exception as e:
        if "busy" in str(e).lower():
            print(f"\n[GPIO ERROR] Failed to claim GPIO pins: {e}")
            print("  ✦ Another instance of 'main.py' is already running!")
            print("  ✦ In modern Linux (Bookworm / lgpio), GPIO pins cannot be opened twice.")
            print("  ✦ Check if the systemd background service is running:")
            print("        sudo systemctl status airplaydevice")
            print("  ✦ To stop the background service so you can test manually in this terminal:")
            print("        sudo systemctl stop airplaydevice")
            print("  ✦ To inspect running python processes:")
            print("        ps aux | grep python")
        else:
            print(f"\n[GPIO ERROR] Button initialization failed: {e}")
        sys.exit(1)

    btns["vol_up"].when_pressed   = on_vol_up
    btns["vol_down"].when_pressed = on_vol_down
    btns["prev"].when_pressed     = on_prev
    btns["next"].when_pressed     = on_next
    btns["play"].when_pressed     = on_play_pause
    btns["vis"].when_pressed      = on_vis_mode
    btns["mode"].when_held        = toggle_mode   # hold RST 1 s to toggle

    # --- Start CAVA audio analyser ---
    start_cava()

    # --- Background threads ---
    threading.Thread(
        target=cava_reader_thread, daemon=True, name="CavaReader"
    ).start()

    threading.Thread(
        target=state_monitor_thread, daemon=True, name="StateMonitor"
    ).start()

    # --- Start LED render loop FIRST (so status LED is alive immediately) ---
    print("[MAIN] Starting LED render loop")
    threading.Thread(
        target=render_loop, daemon=True, name="LEDRender"
    ).start()

    # --- Start TFT display (plays 3s boot splash, then starts render loop) ---
    if TFT_AVAILABLE:
        start_display_threads()   # blocks for 3s during splash
    else:
        print("[MAIN] TFT display module not loaded — skipping")

    # --- Main thread just waits for signals ---
    print("[MAIN] All systems go")
    try:
        signal.pause()   # sleep until SIGTERM / SIGINT
    except KeyboardInterrupt:
        cleanup()
