# Sandy's AirPlay Device — Audio Receiver, Visualizer & TFT Display

A production-grade Raspberry Pi audio receiver, dynamic ST7789 TFT display, real-time frequency visualizer, and hardware media controller.

* **AirPlay Service Name:** `Sandy's Airplay Device`
* **Network Hostname:** `sandy.local`
* **Default SSH Login:** `ssh sandypi@sandy.local`

---

## Hardware Platforms

| Platform | Role | Audio Output | TFT SPI Controller | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Raspberry Pi 4 Model B (1GB)** | **Current Prototype** | 3.5mm Headphone Jack (`bcm2835 Headphones`) | **Hardware SPI4** (`/dev/spidev4.0` @ 32–40 MHz) | 🟢 Active & Fully Calibrated |
| **Raspberry Pi Zero 2 W** | **Production Target** | PCM5102A I2S DAC (`hifiberry-dac` overlay) | Software SPI (`spi-gpio` Bus 1) | 🟡 Target hardware |

The codebase and installation scripts auto-detect the board at runtime, preserving the I2S DAC pinout plan while allowing full prototyping via the Pi 4B's 3.5mm jack.

---

## Key Features & Architecture

### 1. High-Performance ST7789 TFT Display (240×320)
* **Direct Hardware SPI4 Access:** Runs over Raspberry Pi 4B's dedicated hardware SPI controller 4 (`/dev/spidev4.0`) at 32 MHz with direct kernel DMA transfers, bypassing slow bit-banging and preventing GPIO resource contention.
* **Calibrated Color Reproduction:**
  * **RGB Subpixel Mapping:** Configured with `MADCTL` bit 3 = 0, ensuring exact color fidelity without Red/Blue swapping.
  * **Non-Inverted Display (`INVOFF`):** Configured with `INVOFF` (`0x20`), eliminating washed-out contrast and negative color artifacts.
* **Apple SF Pro Rounded Typography Engine:** Rendered with true Apple SF Pro Rounded Bold and SF Compact Rounded typefaces with horizontal ping-pong marquee scrolling and boundary clipping.
* **3 Display Modes (Cycled via Joystick 'SET'):**
  * **Mode 0 (Default):** Cover Art + Animated Sound Wave EQ Badge + Marquee Song Title & Artist + Apple-style Real-Time Progress Bar.
  * **Mode 1:** Edge-to-Edge Full-Screen Album Art + Animated Sound Wave EQ Badge.
  * **Mode 2:** Full-Screen Live Frequency Bar Spectrum Visualizer.
* **Dynamic Equalizer / Sound Wave Status Badge:** Rendered over the bottom-right corner of the album art in Mode 0 and Mode 1. Features 4 animated vertical equalizer bars that dance organically to live CAVA frequency spectrum when playing, and settle flat and motionless when paused for an instant visual status cue.
* **Apple StandBy Idle Screen:** Shows status capsule pill (`AirPlay Ready`), large time, bold Day & Date, audio mode, and Wi-Fi connection indicator.

### 2. Intelligent Real-Time Progress Bar & Scrubbing
* **Intelligent D-Bus Packet Deduplication:** Shairport Sync provides static RTP timestamp snapshots at track start and seek points. Incoming `prgr` strings are dynamically deduplicated against `_last_prgr_string`, preventing periodic 1.0s D-Bus poller queries from clobbering the live monotonic clock back to 0.0s.
* **Dynamic Cache Invalidation:** `_last_prgr_string` is automatically flushed upon stream flush (`pfls`), new track title (`minm`), or explicit skip (`on_track_change()`), ensuring seeks and track changes apply immediately.
* **Apple Pill Scrubber & Dynamic Thumb Indicator:** 6px thick pill track with 8px margins, crisp iOS Blue fill, and a prominent 10px white scrubber thumb with blue rim that is visible and mathematically clamped across the full duration from 0:00 (0%) to song end (100%).
* **Real-Time Monotonic Interpolation:** Advances smoothly at 15 FPS between metadata packets, freezing accurately on pause and resuming seamlessly without time drift.
* **Harmonized Playback State Engine:** Prioritizes explicit D-Bus pause detection (`dbus_status == "paused"`) while keeping playback active during quiet track intros or CAVA startup, eliminating progress bar freezes or flapping.
* **Seek & Mid-Track Position Tracking:**
  * Accurately parses 32-bit unsigned RTP timestamps with epoch rollover and jitter guard (`raw_diff > 0x7FFFFFFF`).
  * Reconciles remaining stream slices (`duration < total - 2.0`) against authoritative song duration (`astm` and D-Bus `mpris:length`) so starting a track mid-song or scrubbing on iOS immediately jumps to the exact timestamp.
  * Preserves metadata across audio flushes (`pfls`), ensuring track title and artwork are never wiped during seeking.
* **Zero-Latency D-Bus Polling:** Synchronizes with `org.gnome.ShairportSync.RemoteControl.ProgressString` and `org.mpris.MediaPlayer2.Player.Metadata` instantly via `_dbus_wake_event`.

### 3. Bit-Perfect Audio & Visualizer Pipeline
* **Shairport Sync (AirPlay):** Bit-perfect AirPlay streaming with metadata pipe (`/tmp/shairport-sync-metadata`), cover art caching (`/tmp/shairport-sync-art`), and 1.0s periodic timeline updates.
* **ALSA Loopback Routing:** Couples audio playback to line output while feeding a zero-lag `dsnoop` capture channel.
* **LED Visualizer (CAVA):** Frequency analysis pipeline powering an 8×8 WS2812B matrix and status indicator over hardware SPI0 MOSI (GPIO 10) to avoid I2S/PWM DMA conflicts.

### 4. 5D Rocker Joystick & Media Control Architecture
* **Single-Dispatch Command Routing:** Eliminates double-toggle glitches on iOS by prioritizing native `org.gnome.ShairportSync.RemoteControl` D-Bus interface and falling back to MPRIS `MediaPlayer2.Player` only if necessary. Pressing Play/Pause, Next, or Prev triggers exactly one action without rapid toggle loops.
* **Instant Optimistic UI Feedback:** Pressing Play/Pause toggles playback state immediately in memory with 0ms perceived latency. The 4-bar equalizer badge instantly settles flat or resumes dancing, and the progress bar freezes or resumes, while D-Bus network commands dispatch asynchronously in a background daemon thread.
* **Snappy Tactile Switch Debouncing:** Button inputs are debounced with a calibrated 80ms window (`BOUNCE_TIME = 0.08`), eliminating button drop, double-clicks, and sluggish feel while filtering physical contact chatter.
* **Decoupled Session & Playback States:** Native `Active` session flag is separated from `PlayerState`/`PlaybackStatus`. When a song is paused, the display stays on Mode 0 or Mode 1 with the flat EQ badge and frozen progress bar rather than prematurely jumping to the clock screen.
* **Metadata Pause Stability & Disconnect Purging:** Metadata ingestion (`minm`, `asar`, `pcen`, `PICT`) preserves authoritative playback state without false unpausing. On explicit session termination or `Stopped` status, stale metadata and cached artwork are purged instantly, returning the display to the StandBy clock.
* **Multi-Card ALSA SoftMaster Volume Fallback:** Physical rocker Volume Up / Down calls probe candidate cards (`Headphones`, `sndrpihifiberry`, `0`, `1`, `2`) if the default card mixer does not expose `SoftMaster`, guaranteeing smooth volume adjustments across RPi 4B and Pi Zero 2W.
* **Fault-Tolerant Rendering & Display Loop Resilience:** All visualizer routines clamp minimum bar counts (`num_bars = max(1, len(cava_vals))`) to prevent `ZeroDivisionError` on startup or empty pipes. The main display daemon thread in `DisplayController.run()` is wrapped in a resilient exception handler, ensuring the ST7789 display never freezes or terminates.
* **Dual-Stack Wi-Fi Introspection:** The StandBy clock queries both `iwgetid -r` and `nmcli -t -f active,ssid dev wifi`, displaying active SSID on both legacy and NetworkManager-based Bookworm installations.

---

## Hardware Wiring Reference

Both the Raspberry Pi 4B and Raspberry Pi Zero 2W share the 40-pin GPIO header.

### 1. ST7789 TFT Display (2.8" 240×320) — Hardware SPI4 (RPi 4B)

| TFT Pin | Function | RPi GPIO | Physical Pin # | Wire Color | Notes |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **VCC** | Power | 3V3 | **Pin 1** | **🔴 Red** | 3.3V Logic |
| **GND** | Ground | GND | **Pin 6** | **🟤 Brown** | System ground |
| **CS** | Chip Select | **GPIO 25** | **Pin 22** | **🟠 Orange** | SPI4 CS0 |
| **RESET** | Hardware Reset | **GPIO 12** | **Pin 32** | **🟡 Yellow** | Direct GPIO control |
| **DC** | Data / Command | **GPIO 24** | **Pin 18** | **🟢 Green** | Direct GPIO control |
| **SDI (MOSI)**| Serial Data In | **GPIO 6** | **Pin 31** | **🔵 Blue** | SPI4 MOSI |
| **SCK** | Serial Clock | **GPIO 7** | **Pin 26** | **🟣 Purple** | SPI4 SCLK *(moved from Pin 29)* |
| **LED** | Backlight Control | **GPIO 20** | **Pin 38** | **⚪ Grey** | Active-high |
| *SDO / Touch* | Unused | — | — | — | Leave disconnected |

> **✦ Note on SPI4 SCLK (Pin 26 / GPIO 7):**
> On Raspberry Pi 4B, `dtoverlay=spi0-1cs` restricts SPI0 to CS0 (GPIO 8), freeing GPIO 7 (CE1) so that `dtoverlay=spi4-1cs,cs0_pin=25` claims GPIO 7 as the hardware SCLK line. This provides 32–40 MHz hardware DMA.

### 2. 5D Rocker Joystick (Media & Mode Controls)

| Rocker Pin | Function | RPi GPIO | Physical Pin # | Wire Color | Default Action |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **COM** | Common Ground | GND | **Pin 14** | **⚫ Black** | Shared ground |
| **UP** | Volume Up | **GPIO 16** | **Pin 36** | **⚪ White** | Increases ALSA volume |
| **DOWN** | Volume Down | **GPIO 17** | **Pin 11** | **🔘 Grey** | Decreases ALSA volume |
| **LEFT** | Previous Track | **GPIO 22** | **Pin 15** | **🟣 Purple** | AirPlay Prev Track |
| **RIGHT** | Next Track | **GPIO 23** | **Pin 16** | **🔵 Blue** | AirPlay Next Track |
| **MID** | Play / Pause | **GPIO 26** | **Pin 37** | **🟢 Green** | Toggle Play / Pause |
| **SET** | Cycle Modes | **GPIO 27** | **Pin 13** | **🟡 Yellow** | Info → Art → Visualizer |
| **RST** | Toggle AirPlay/BT | **GPIO 4** | **Pin 7** | **🟠 Orange** | Long-press (1.0s) |

### 3. WS2812B LEDs (Matrix & Status)

| LED Pin | Connection | RPi GPIO | Physical Pin # | Notes |
| :--- | :--- | :--- | :---: | :--- |
| **5V / VCC** | External 5V PSU | — | — | **Do NOT power from Pi 5V pin!** |
| **GND** | Common Ground | GND | **Pin 39** | Common ground with Pi |
| **DIN** | SPI0 MOSI | **GPIO 10** | **Pin 19** | Avoids PWM/I2S DMA conflict |

---

## Audio Pipeline

```text
AirPlay (shairport-sync) / Bluetooth (bluealsa)
       ↓
hw:Loopback,0,0 (ALSA Loopback Playback)
       ↕ (kernel coupled)
hw:Loopback,1,0 (ALSA Loopback Capture)
       ├─→ alsaloop → softvol_out → 3.5mm Headphone Jack (or PCM5102A DAC)
       └─→ CAVA (FIFO /tmp/cava.fifo) → WS2812B LED Matrix
```

---

## Installation & Setup

### 1. Clone & Install
```bash
git clone https://github.com/Sandeep271958/The_Airplay_Device.git ~/Airplaydevice
cd ~/Airplaydevice
bash install.sh
```

### 2. Boot Configuration (`/boot/firmware/config.txt`)
Ensure the following overlays are enabled in `/boot/firmware/config.txt`:

```ini
# Audio configuration
dtparam=audio=on
hdmi_ignore_edid_audio=1

# Enable hardware SPI
dtparam=spi=on

# Restrict SPI0 to 1 CS (GPIO 8) — frees GPIO 7 for SPI4 SCLK
dtoverlay=spi0-1cs

# Enable Hardware SPI4 for ST7789 TFT Display (32 MHz DMA)
dtoverlay=spi4-1cs,cs0_pin=25
```

### 3. Reboot
```bash
sudo reboot
```

---

## Verification & Automated Test Suite

A comprehensive automated test suite validates all 10 core subsystems:

```bash
cd ~/Airplaydevice
sudo venv/bin/python test_suite.py
```

### Test Suite Modules (32 Comprehensive Tests — 100% Pass Rate):
1. **System Environment & Daemons:** Checks root permissions and active systemd units (`shairport-sync`, `airplaydevice`, `avahi-daemon`).
2. **Audio & ALSA Subsystem:** Validates `snd-aloop` kernel module, 3.5mm DAC device, and `SoftMaster` mixer control.
3. **CAVA Audio FIFO:** Validates `/tmp/cava.fifo` named pipe node and permissions.
4. **Shairport Sync D-Bus:** Queries `org.gnome.ShairportSync` and `org.mpris.MediaPlayer2`, testing playback/connection decoupling and clean session disconnect/stopped resolution.
5. **Metadata & Album Art:** Tests basic fields, JPEG decoding, stale cache clearing, `on_track_change()` reset, chunked assembly (`pcst` → `PICT` → `pcen`), and pause stability during metadata reception.
6. **Progress Bar & Timeline Engine:**
   * 32-bit uint32 RTP timestamp rollover handling.
   * Monotonic 15 FPS real-time clock interpolation (`now - update_time`).
   * Pause clock freeze accuracy (verifies 0.000s drift).
   * High-visibility Apple pill track & scrubber thumb rendering.
   * Mid-track start & seek offset calculation (`offset = total - duration`).
   * Stream flush (`pfls`) metadata and progress preservation.
   * DAAP total song duration (`astm`) 4-byte big-endian parsing.
   * Repeated identical `prgr` packet deduplication (prevents D-Bus clock resets).
   * 10-packet burst storm resilience.
   * Bi-directional seek & scrub geometry (forward to 140s, backward to 30s).
   * Scrubber thumb boundary clamp math at 0%, 50%, and 100%.
   * Dynamic equalizer sound wave badge rendering (active dancing bars on play, flat motionless state on pause).
   * Zero-division & empty CAVA spectrum resilience across all display modes.
7. **Typography Assets:** Verifies SF Pro Rounded Bold, SF Compact Rounded, and logo assets.
8. **Marquee Engine:** Checks ping-pong scrolling offsets, pause intervals, and boundary clamping.
9. **TFT Hardware & Calibration:** Verifies `/dev/spidev4.0`, RGB subpixel mode (`MADCTL`), and non-inverted colors (`INVOFF`).
10. **5D Rocker Joystick:** Validates GPIO claim and pull-up resistor health across all 7 control lines, single-dispatch D-Bus routing, and 80ms debounce calibration.

### Standalone Display Calibration Tool
To inspect or re-calibrate the physical ST7789 display independently:
```bash
# Calibrated production mode (RGB + INVOFF):
sudo venv/bin/python test_tft.py

# Diagnostic switches:
sudo venv/bin/python test_tft.py --rgb     # Force RGB subpixel ordering
sudo venv/bin/python test_tft.py --bgr     # Force BGR subpixel ordering
sudo venv/bin/python test_tft.py --invoff  # Force Inversion OFF
sudo venv/bin/python test_tft.py --invon   # Force Inversion ON
```

---

## Service Management

All components are managed via systemd:

```bash
# Check service status
sudo systemctl status shairport-sync
sudo systemctl status alsaloop
sudo systemctl status airplaydevice

# Restart after changes
sudo systemctl restart shairport-sync
sudo systemctl restart airplaydevice

# View real-time logs
sudo journalctl -u airplaydevice -f
```
