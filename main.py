#!/usr/bin/env python3
"""
AirPlay / Bluetooth Audio Receiver & Visualizer Controller
===========================================================
Raspberry Pi Zero 2W

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
    from tft_display import display_controller, start_display_threads
    TFT_AVAILABLE = True
except ImportError:
    TFT_AVAILABLE = False
    print("[TFT] tft_display module not found — display disabled")


# =====================================================================
# CONFIGURATION
# =====================================================================

# --- WS2812 LEDs (SPI mode on GPIO 10 = SPI0 MOSI) ---
# We MUST use SPI mode because PWM0 shares hardware with the I2S DAC.
# GPIO 10 triggers the rpi_ws281x SPI driver automatically.
LED_COUNT       = 65                    # 1 status LED + 64 matrix LEDs
LED_PIN         = 10                    # GPIO 10 (SPI0 MOSI)
LED_FREQ_HZ     = 800000
LED_DMA         = 10
LED_BRIGHTNESS  = 80                    # 0–255; keep moderate to limit current
LED_INVERT      = False
LED_CHANNEL     = 0
LED_STRIP_TYPE  = ws.WS2811_STRIP_GRB   # WS2812B native colour order is GRB

# Pixel layout: index 0 = status LED, indices 1–64 = 8×8 matrix

# --- 5D Rocker Joystick (BCM pin numbers, active-low via internal pull-up) ---
PIN_VOL_UP      = 16    # UP     → Volume Up
PIN_VOL_DOWN    = 17    # DOWN   → Volume Down
PIN_PREV        = 22    # LEFT   → Previous Track
PIN_NEXT        = 23    # RIGHT  → Next Track
PIN_PLAY_PAUSE  = 26    # MID    → Play / Pause
PIN_VIS_MODE    = 27    # SET    → Cycle Visualizer Modes
PIN_MODE_TOGGLE = 4     # RST    → Toggle AirPlay ↔ Bluetooth (hold 1 s)

BOUNCE_TIME     = 0.15  # seconds — mechanical switch debounce
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
    print("[LED]  Strip initialised (SPI mode, GPIO 10, 65 pixels)")
except RuntimeError as e:
    print(f"[LED]  FATAL — cannot initialise WS2812: {e}")
    print("       ✦ Is SPI enabled?  sudo raspi-config → Interface Options → SPI")
    print("       ✦ Running as root? sudo python3 main.py")
    sys.exit(1)


# =====================================================================
# COLOUR HELPERS & PALETTE
# =====================================================================

C = ws.Color   # shorthand: C(red, green, blue)

OFF   = C(0, 0, 0)
WHITE = C(255, 255, 255)
CYAN  = C(0, 255, 255)

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
# STATUS LED  (pixel 0)
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
# 8×8 MATRIX VISUALISER  (pixels 1–64)
# =====================================================================

def write_matrix_bars(values):
    """
    Draw 8 frequency-bar columns onto pixels 1–64.

    `values`  — list of 8 ints in range 0–255.

    Assumes standard WS2812B 8×8 panel with serpentine (zig-zag) wiring:
      Even columns (0, 2, 4, 6): data flows bottom → top
      Odd  columns (1, 3, 5, 7): data flows top → bottom
    """
    for col in range(8):
        height = min(8, int(values[col] / 255.0 * 8))
        for row in range(8):
            # Serpentine index (+1 offset because pixel 0 is the status LED)
            if col % 2 == 0:
                idx = 1 + col * 8 + row           # bottom → top
            else:
                idx = 1 + col * 8 + (7 - row)     # top → bottom

            strip.setPixelColor(
                idx,
                BAR_GRADIENT[row] if row < height else OFF,
            )


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


# =====================================================================
# UNIFIED RENDER LOOP  (~30 FPS)
# =====================================================================

def render_loop():
    """
    Single loop that updates the status LED AND the matrix, then calls
    strip.show() exactly once per frame.

    This eliminates the flickering that occurs when two separate threads
    each call strip.show() independently — show() pushes the entire
    pixel buffer to hardware, so concurrent callers stomp on each other.
    """
    step = 0.0
    interval = 1.0 / TARGET_FPS

    while True:
        t0 = time.monotonic()

        with strip_lock:
            # 1. Status LED (pixel 0)
            colour, step = compute_status_colour(step)
            strip.setPixelColor(0, colour)

            # 2. Matrix (pixels 1–64)
            with _cava_lock:
                vals = list(_cava_vals)
            write_matrix_bars(vals)

            # 3. Push entire 65-pixel buffer to hardware in one call
            strip.show()

        # 4. Feed CAVA data to the TFT display controller
        if TFT_AVAILABLE:
            display_controller.set_cava_vals(vals)

        elapsed = time.monotonic() - t0
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)


# =====================================================================
# STATE MONITOR  (background thread)
# =====================================================================

def state_monitor_thread():
    """
    Poll connection/playback state every 2 seconds.

    AirPlay mode : reads /tmp/airplay_state (written by shairport-sync hooks).
    Bluetooth mode: queries bluetoothctl for connected devices.
    """
    while True:
        try:
            if state.mode == AppState.MODE_AIRPLAY:
                if os.path.exists(STATE_FILE):
                    with open(STATE_FILE, "r") as f:
                        val = f.read().strip()
                    if val == "playing":
                        state.connected = True
                        state.playing   = True
                    elif val == "connected":
                        state.connected = True
                        state.playing   = False
                    else:   # "disconnected" or anything unexpected
                        state.connected = False
                        state.playing   = False
                else:
                    state.connected = False
                    state.playing   = False

            else:  # Bluetooth mode
                result = subprocess.run(
                    ["bluetoothctl", "info"],
                    capture_output=True, text=True, timeout=3,
                )
                if "Connected: yes" in result.stdout:
                    state.connected = True
                    state.playing   = True   # assume playing once connected
                else:
                    state.connected = False
                    state.playing   = False

        except Exception:
            pass

        # Sync state to TFT display
        if TFT_AVAILABLE:
            display_controller.set_playing(state.playing)
            display_controller.set_connected(state.connected)

        time.sleep(2)


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
            track_meta.clear()  # don't show stale Bluetooth metadata
        print("[MODE] AIRPLAY active — waiting for connection…")


# =====================================================================
# MEDIA CONTROL CALLBACKS
# =====================================================================

def on_vol_up():
    """Increase volume via ALSA softvol control."""
    print("[BTN] Volume ▲")
    subprocess.run(["amixer", "sset", "SoftMaster", "5%+"], check=False)


def on_vol_down():
    """Decrease volume via ALSA softvol control."""
    print("[BTN] Volume ▼")
    subprocess.run(["amixer", "sset", "SoftMaster", "5%-"], check=False)


def on_next():
    """Skip to next track (Bluetooth only — AirPlay is sender-controlled)."""
    print("[BTN] Next ▶▶")
    if state.mode == AppState.MODE_BLUETOOTH:
        subprocess.run(["playerctl", "next"], check=False)


def on_prev():
    """Skip to previous track (Bluetooth only)."""
    print("[BTN] Prev ◀◀")
    if state.mode == AppState.MODE_BLUETOOTH:
        subprocess.run(["playerctl", "previous"], check=False)


def on_play_pause():
    """Toggle play / pause (Bluetooth only)."""
    print("[BTN] Play/Pause ⏯")
    if state.mode == AppState.MODE_BLUETOOTH:
        subprocess.run(["playerctl", "play-pause"], check=False)


def on_vis_mode():
    """Cycle through visualiser / display modes."""
    new_mode = (state.vis_mode + 1) % 3
    state.vis_mode = new_mode
    names = ["Art + Info", "Full-screen Art", "Live Visualiser"]
    print(f"[BTN] Display mode → {names[new_mode]}")
    if TFT_AVAILABLE:
        display_controller.set_mode(new_mode)


# =====================================================================
# CLEAN SHUTDOWN
# =====================================================================

def cleanup(sig=None, frame=None):
    """Turn off all LEDs, stop CAVA, and exit."""
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
    sys.exit(0)


# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":
    print("=" * 58)
    print("  AirPlay / Bluetooth Audio Receiver & Visualiser")
    print("  Raspberry Pi Zero 2W")
    print("=" * 58)

    # Catch SIGTERM (from systemd stop) and SIGINT (Ctrl+C)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT,  cleanup)

    # --- Initialise 5D Rocker buttons with debouncing ---
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
