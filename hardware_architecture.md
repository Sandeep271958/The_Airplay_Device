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

### 3.3. WS2812B LEDs (8×8 Matrix & Status Indicator)

| LED Pin | Connection | RPi GPIO | Physical Pin # | Hardware Note |
| :---: | :--- | :---: | :---: | :--- |
| **5V / VCC** | External 5V PSU | — | — | **Do NOT draw from Pi 5V pin!** |
| **GND** | PSU GND + Pi GND | GND | **Pin 39** | Common ground required |
| **DIN** | SPI0 MOSI | **GPIO 10** | **Pin 19** | Avoids PWM/I2S DMA conflict |

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

On pause (`paus` / `pend`), the monotonic accumulator is frozen instantly.

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
