# Sandy's AirPlay Device — Hardware & Software Architecture

## 1. System Overview & Audio Strategy

Sandy's AirPlay Device is a dedicated, high-fidelity wireless audio receiver with real-time frequency analysis, dynamic ST7789 TFT album art and track information display, and physical 5D rocker controls.

* **AirPlay Receiver:** `shairport-sync` 4.x running bit-perfect ALSA output.
* **Audio Interconnect:** ALSA Loopback driver (`snd-aloop`) decoupling the audio stream so both playback and spectrum analysis tap the same audio buffer concurrently without jitter.
* **Prototyping Platform:** Raspberry Pi 4 Model B (1GB) using the onboard 3.5mm analog output (`bcm2835 Headphones`).
* **Production Target:** Raspberry Pi Zero 2 W with a PCM5102A I2S DAC (`hifiberry-dac` overlay).

---

## 2. Bill of Materials & Equipment List

| Component | Description & Specification | Purpose |
| :--- | :--- | :--- |
| **Raspberry Pi 4 Model B** | 1GB / 64-bit ARMv8 Cortex-A72 | Prototype processing engine (Wi-Fi, AirPlay, DSP, SPI4). |
| **Raspberry Pi Zero 2 W** | Quad-core ARM Cortex-A53 | Production target hardware. |
| **ST7789 2.8" SPI TFT LCD** | 240×320 IPS display, 4-wire SPI | Real-time album art, track details, scrubber bar, and StandBy clock. |
| **5D Rocker Joystick** | 5-way digital switch + SET / RST | Volume, track skipping, play/pause, mode cycle, source toggle. |
| **WS2812B 8×8 LED Matrix** | 64 RGB Addressable LEDs | Real-time audio spectrum frequency visualizer (CAVA). |
| **WS2812B Status LED** | 1 Single RGB Addressable LED | Connection state indicator (AirPlay / BT). |
| **PCM5102A I2S DAC** | 32-bit / 384 kHz stereo DAC | Line-level high-fidelity analog audio output (production target). |
| **Power Supply** | 5V 4A+ DC regulated PSU | Prevents voltage sag during heavy LED matrix illumination. |

---

## 3. GPIO Header Pinout & Bus Allocation

To avoid hardware resource contention across DMA, PWM, and I2S:
* **Audio DAC:** I2S interface on GPIO 18 (BCLK), 19 (LRCK), and 21 (DIN).
* **WS2812B LEDs:** Hardware SPI0 MOSI (GPIO 10 / Pin 19) — avoids PWM0 conflicts with I2S audio.
* **ST7789 TFT Display:** Dedicated **Hardware SPI4** (`/dev/spidev4.0` @ 32–40 MHz) on RPi 4B; software SPI on Pi Zero 2W.
* **5D Rocker Joystick:** 7 standard digital GPIO inputs with internal software pull-up resistors.

### 3.1. ST7789 TFT LCD Wiring (RPi 4B Hardware SPI4)

| TFT Module Pin | Function | RPi GPIO | Physical Pin # | Wire Color | Bus Assignment |
| :---: | :--- | :---: | :---: | :---: | :--- |
| **VCC** | 3.3V Power | 3V3 | **Pin 1** | **🔴 Red** | 3.3V Rail |
| **GND** | System Ground | GND | **Pin 6** | **🟤 Brown** | System Ground |
| **CS** | Chip Select | **GPIO 25** | **Pin 22** | **🟠 Orange** | SPI4 CS0 (`cs0_pin=25`) |
| **RESET** | Display Reset | **GPIO 12** | **Pin 32** | **🟡 Yellow** | Direct GPIO |
| **DC** | Data / Command | **GPIO 24** | **Pin 18** | **🟢 Green** | Direct GPIO |
| **SDI (MOSI)** | Serial Data In | **GPIO 6** | **Pin 31** | **🔵 Blue** | SPI4 MOSI |
| **SCK** | Serial Clock | **GPIO 7** | **Pin 26** | **🟣 Purple** | SPI4 SCLK *(moved from Pin 29)* |
| **LED** | Backlight Control | **GPIO 20** | **Pin 38** | **⚪ Grey** | Active-High GPIO |

> **✦ Why SPI4 on RPi 4B?**
> The Raspberry Pi 4B features additional hardware SPI controllers (SPI3 through SPI6). By using `dtoverlay=spi0-1cs`, SPI0's second chip-select (GPIO 7 / CE1) is released, allowing `dtoverlay=spi4-1cs,cs0_pin=25` to configure GPIO 6 (MOSI), GPIO 7 (SCLK), and GPIO 25 (CS0) as a 40 MHz DMA hardware controller. This eliminates CPU-bound bitbanging.

### 3.2. 5D Rocker Joystick (Media Controls)

All inputs configure active-low with internal pull-ups (`pull_up=True`):

| Rocker Pin | Function | RPi GPIO | Physical Pin # | Wire Color | Software Action |
| :---: | :--- | :---: | :---: | :---: | :--- |
| **COM** | Common Ground | GND | **Pin 14** | **⚫ Black** | Digital ground |
| **UP** | Volume Up | **GPIO 16** | **Pin 36** | **⚪ White** | `SoftMaster` ALSA +5% |
| **DOWN** | Volume Down | **GPIO 17** | **Pin 11** | **🔘 Grey** | `SoftMaster` ALSA -5% |
| **LEFT** | Previous Track | **GPIO 22** | **Pin 15** | **🟣 Purple** | Shairport Sync Prev |
| **RIGHT** | Next Track | **GPIO 23** | **Pin 16** | **🔵 Blue** | Shairport Sync Next |
| **MID** | Play / Pause | **GPIO 26** | **Pin 37** | **🟢 Green** | Toggle Play / Pause |
| **SET** | Cycle Display Modes | **GPIO 27** | **Pin 13** | **🟡 Yellow** | Info ↔ Art ↔ Visualizer |
| **RST** | Toggle Mode | **GPIO 4** | **Pin 7** | **🟠 Orange** | AirPlay ↔ Bluetooth (1.0s hold) |

### 3.3. WS2812B LEDs (8×8 Matrix & Status Indicator Daisy-Chain)

The 8×8 Matrix and the dedicated single Status LED are daisy-chained on a single high-speed SPI data line (SPI0 MOSI on GPIO 10 / Physical Pin 19):

```text
Raspberry Pi 4B Pin 19 (GPIO 10 / SPI0 MOSI)
       │
       ▼
┌───────────────────────────────┐
│     8×8 Matrix LED Plate      │  Pixels 0–63 (64 LEDs)
│  (DIN on Pin 19 / GPIO 10)    │  Serpentine zig-zag column mapping
│             DOUT              │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│   Single WS2812B Status LED   │  Pixel 64 (1 LED)
│  (DIN attached to Matrix DOUT)│  AirPlay / Bluetooth status indicator
└───────────────────────────────┘
```

| Component | Pin | Connection | RPi Pin / Source | Hardware Notes |
| :--- | :---: | :--- | :---: | :--- |
| **8×8 Matrix Plate** | **5V / VCC** | External 5V PSU | External PSU | **Do NOT power from Pi 5V pin!** |
| | **GND** | Common Ground | **Pin 39** + PSU GND | Common ground required |
| | **DIN** | SPI0 MOSI | **Pin 19** (GPIO 10) | Pixels 0–63 (Avoids PWM/I2S DMA conflict) |
| | **DOUT** | Daisy-chain Out | Status LED DIN | Carries data to Pixel 64 |
| **Single Status LED** | **5V / VCC** | 5V Supply | 5V Rail / PSU | Power supply |
| | **GND** | Common Ground | Common GND | Shared ground |
| | **DIN** | Signal In | Matrix **DOUT** | Maps to **Pixel 64** |

#### Thermal Protection & Power Optimization
* **50% Brightness Calibration (`LED_BRIGHTNESS = 40`):** WS2812B matrices generate intense heat when all 64 LEDs glow at high brightness on a small PCB. Master brightness is set to 40 (50% reduction from 80), providing vibrant, rich colors while running cool and safe.
* **Idle / Pause Matrix Blanking (`clear_matrix_bars()`):** When playback is paused, stopped, or disconnected, all 64 matrix pixels are powered off immediately. Only the dedicated Status LED (Pixel 64) gently pulses or glows, eliminating thermal buildup and conserving power.

---

## 4. ST7789 Display Pipeline & Color Calibration

The direct hardware driver in `tft_display.py` interacts with the LCD controller via:

1. **Hardware Reset:** Pull GPIO 12 LOW for 100ms, then HIGH for 120ms (`CMD_SWRESET = 0x01`, `CMD_SLPOUT = 0x11`).
2. **Subpixel Mapping (RGB Mode):**
   * ST7789 `MADCTL` register (`0x36`) bit 3 controls RGB vs BGR order.
   * Calibrated with `MADCTL = 0x00` (or `0x08` bit 3 = 0), matching the physical IPS LCD subpixel stripe.
3. **Display Inversion (INVOFF):**
   * Many generic ST7789 panels have inverted IPS polarizers.
   * Initialized with `INVOFF` (`0x20`) rather than `INVON` (`0x21`) to prevent washed-out, milky colors and negative contrast.
4. **Color Mode:** 16-bit RGB565 (`CMD_COLMOD = 0x3A`, parameter `0x55`).

---

## 5. Timeline Synchronization & Seek Mechanics

### 5.1. The AirPlay RTP Progress Metric
AirPlay transmits timing via 32-bit unsigned RTP timestamps running at **44,100 Hz**:

$$\text{prgr} = \text{start\_rtp} / \text{curr\_rtp} / \text{end\_rtp}$$

* **Rollover Math:** Differences are calculated modulo $2^{32}$ (`& 0xFFFFFFFF`):
  $$\text{dur\_frames} = (\text{end\_rtp} - \text{start\_rtp}) \ \& \ \text{0xFFFFFFFF}$$
  $$\text{elapsed\_frames} = (\text{curr\_rtp} - \text{start\_rtp}) \ \& \ \text{0xFFFFFFFF}$$
* **Jitter Guard:** If $(\text{curr\_rtp} - \text{start\_rtp}) > \text{0x7FFFFFFF}$, the difference represents pre-roll buffer jitter where the playhead is slightly ahead of the start frame. Clamped to 0.0s to avoid integer underflow jumping to 97,000 seconds.

### 5.2. Stream Slice Reconciliation (Mid-Track Start & Scrubbing)
When playback begins mid-track (e.g. at 1:15 of a 3:00 track) or the user scrubs on iOS:
1. AirPlay emits a stream flush (`pfls`).
2. The receiver preserves track metadata (`title`, `artist`, `art_img`) and wakes the D-Bus poller.
3. AirPlay streams a slice where `duration` represents the *remaining* seconds ($105\text{s}$) rather than total duration ($180\text{s}$).
4. The receiver correlates this against the authoritative duration from DAAP `astm` or D-Bus `mpris:length`:
   $$\text{offset} = \text{total\_duration} - \text{duration}$$
   $$\text{elapsed} = \text{offset} + \text{elapsed}$$
5. The progress bar thumb instantly jumps to $75.0\text{s}$ ($41.7\%$) without resetting to zero.

### 5.3. Real-Time Monotonic Interpolation
Between discrete metadata packets (sent every 1.0s via `progress_interval = 1.0`), the snapshot engine advances elapsed time continuously using Python's high-resolution monotonic clock:

$$\text{elapsed}(t) = \text{elapsed\_base} + (t_{\text{monotonic}} - t_{\text{update}})$$

On pause (`paus` / `pend`), the monotonic accumulator is frozen instantly without drift.

### 5.4. D-Bus Packet Deduplication & Polling Protection
Shairport Sync exposes `ProgressString` over D-Bus as `start_rtp / curr_rtp / end_rtp`. Crucially, AirPlay transmits static timestamp snapshots rather than second-by-second updates. Querying D-Bus at 1.0s cadence would repeatedly re-anchor $\text{elapsed\_base} = 0.0$ and reset $t_{\text{update}} = t_{\text{monotonic}}$, locking the progress bar to 0%.

To solve this:
1. `_process_prgr_string()` compares incoming payload strings against `_last_prgr_string`.
2. Identical duplicate packets are discarded immediately, allowing $t_{\text{monotonic}}$ to interpolate smoothly.
3. The deduplication anchor `_last_prgr_string` is dynamically cleared on:
   * Stream flush (`pfls`) — emitted when seeking or skipping.
   * New track title (`minm` / `xesam:title`).
   * Explicit track change (`on_track_change()` / `clear()`).

### 5.5. Scrubber Thumb Indicator & Boundary Clamp Math
The progress bar renders a 6px pill background (`PROGRESS_BG = (228, 232, 240)`) with an Apple-style white scrubber thumb (`dot_r = 5px`) and iOS Blue rim (`PROGRESS_FG = (0, 122, 255)`).

The thumb position is mathematically clamped within track boundaries to ensure seamless visual rendering at both track start ($0\%$) and track end ($100\%$):

$$\text{dot\_cx} = \text{clamp}\Big(x + \text{radius}, \ x + \text{int}(w \cdot \text{frac}), \ x + w - \text{radius}\Big)$$

For display width $240\text{px}$, bar origin $x=8$, width $w=224$, and corner radius $=3$:
* At $0\%$ ($0\text{s}$ elapsed): $\text{dot\_cx} = 8 + 3 = 11\text{px}$.
* At $50\%$ (mid-track): $\text{dot\_cx} = 8 + 112 = 120\text{px}$.
* At $100\%$ (track end): $\text{dot\_cx} = 8 + 224 - 3 = 229\text{px}$.

### 5.6. Playback State Harmonization Engine
The multi-threaded architecture harmonizes playback state between ALSA Loopback audio energy, D-Bus MPRIS status, Shairport Sync session hooks, and the TFT metadata reader:
* **Explicit Pause:** When `dbus_status == "paused"`, `is_playing` evaluates immediately to `False`, halting monotonic clock advancement.
* **Active Playback:** When `dbus_status == "playing"`, CAVA audio energy $> 10$, or session hook file reports `playing`, `is_playing = True`.
* **Quiet Intro Guard:** When audio energy is low during quiet intro passages, active metadata (`track_meta.is_playing`) maintains playback state without flapping or freezing.

### 5.7. Dynamic Equalizer Sound Wave Status Badge
An Apple Music / Spotify-style miniature audio spectrum badge is rendered over the bottom-right corner of the album art in Mode 0 (art + info) and Mode 1 (fullscreen art):
* **Capsule Overlay:** 30×22px translucent frosted-glass capsule (`RGBA(10, 14, 24, 185)`) with a subtle 1px border (`RGBA(255, 255, 255, 55)`) and 6px rounded corners.
* **Active Animation (Playing):** 4 vertical white bars (`RGBA(255, 255, 255, 240)`) dance smoothly. Heights are computed by blending live CAVA frequency bands ($0, 2, 4, 6$) with organic procedural phase-shifted sine oscillations:
  $$h_i = \text{clamp}\Big(2, \ \text{max\_h} \cdot \max(0.25, \ 0.7 \cdot |\sin(\omega_i t + \phi_i)| + 0.3 \cdot \text{cava}_i), \ \text{max\_h}\Big)$$
* **Flat Motionless State (Paused):** When `is_playing == False`, all 4 bars instantly flatten to a still $2\text{px}$ baseline bar ($h_i = 2$), providing an unmistakable visual playback cue without relying solely on the status LED.

### 5.8. Single-Dispatch Media Control & Optimistic UI Architecture
The 5D tactile rocker switch interfaces directly with Shairport Sync over D-Bus with production-level responsiveness:
* **Single-Dispatch Command Routing:** Previous iterations fired identical commands sequentially to both `org.gnome.ShairportSync.RemoteControl` and `org.mpris.MediaPlayer2.Player`. Because both interfaces are hosted in the same daemon, iOS received two DACP packets in rapid succession (< 50ms), causing immediate toggle cancellation (e.g. Pause followed instantly by Resume). The unified `_send_shairport_command()` dispatches strictly to the primary RemoteControl interface, falling back to MPRIS only on a non-zero exit code.
* **Instant Optimistic UI Feedback:** Pressing MID (Play/Pause) updates local playback state in memory immediately before dispatching the D-Bus network call. This produces $0\text{ms}$ perceived visual latency: the 4-bar equalizer badge instantly settles flat or resumes dancing, and the progress bar freezes or resumes without waiting for the 1.0s background D-Bus poller.
* **Non-Blocking Asynchronous Workers:** Media commands and ALSA volume adjustments are handed off to daemon threads (`threading.Thread`), preventing blocking subprocess calls from starving `gpiozero`'s worker thread.
* **Debounce Calibration:** Tactile rocker microswitch inputs are filtered with `BOUNCE_TIME = 0.08` (80ms), providing crisp, responsive clicking without dropped presses or mechanical contact bounce.
* **Decoupled Session vs Playback State:** The native `org.gnome.ShairportSync.Active` boolean property indicates active AirPlay client connection rather than playback status. Decoupling connection state from `PlayerState` / `PlaybackStatus` ensures that paused tracks remain on the album art screen (Mode 0 / Mode 1) with the flat EQ badge rather than prematurely reverting to the idle clock screen.

### 5.9. Fault-Tolerant Rendering & Display Loop Exception Guard
To ensure production-level 24/7 reliability on embedded Raspberry Pi hardware:
* **Zero-Division Spectrum Guard:** Both full-screen visualiser (`render_visualiser`) and embedded mini-visualiser (`_draw_mini_vis`) enforce safe bar counts ($\text{num\_bars} = \max(1, \text{len}(\text{cava\_vals}))$) and fallback default arrays ($[0] \times 8$). This prevents runtime `ZeroDivisionError` when CAVA is restarting or before the first audio packet arrives.
* **Display Daemon Loop Isolation:** The frame generation and hardware SPI transmission loop in `DisplayController.run()` is wrapped in an isolated exception block. Any transient rendering anomaly or corrupt payload logs a message and sleeps briefly ($0.5\text{s}$) rather than terminating the background thread, ensuring the physical display never freezes permanently.

### 5.10. Multi-Card ALSA SoftMaster Mixer Fallback
The PCM5102A I2S DAC and RPi 4B 3.5mm jack lack fixed hardware mixers, relying on ALSA's `softvol` plugin to synthesize the `SoftMaster` control:
* Because `softvol` binds controls directly to the target hardware card (`card Headphones` or `card sndrpihifiberry`), calling `amixer sset SoftMaster` against the default card may fail if ALSA defaults point to an HDMI audio node.
* The volume dispatchers `on_vol_up()` and `on_vol_down()` automatically fall back to probing candidate cards (`Headphones`, `sndrpihifiberry`, `0`, `1`, `2`) via `amixer -c <card> sset SoftMaster 5%±`, ensuring reliable volume steps regardless of default card index.

### 5.11. Dual-Stack Wi-Fi SSID Introspection (Bookworm / NetworkManager)
The StandBy clock screen displays the currently connected Wi-Fi network:
* On legacy systems, `iwgetid -r` extracts the SSID via `wireless-tools`.
* On modern Raspberry Pi OS (Debian 12 Bookworm) installations where NetworkManager is default, `iwgetid` may return empty. The introspection engine falls back to `nmcli -t -f active,ssid dev wifi`, guaranteeing the SSID badge renders accurately across both network managers.

---

## 6. Audio Routing Pipeline

```
AirPlay (shairport-sync) / Bluetooth (bluealsa)
       ↓ writes S16_LE 44100Hz stereo
hw:Loopback,0,0 (ALSA Loopback Playback)
       ↕ coupled internally by kernel
hw:Loopback,1,0 (ALSA Loopback Capture)
       ↓ shared via dsnoop (pcm.loopcapture)
       ├── CAVA (FIFO /tmp/cava.fifo) → WS2812B 8×8 Matrix
       └── alsaloop → pcm.softvol_out → 3.5mm Jack (bcm2835 Headphones)
```
