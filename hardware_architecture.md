# AirPlay Audio Receiver & Visualizer — Hardware Architecture

## 1. Audio Output Strategy
The Raspberry Pi Zero 2W does not have a built-in analog audio output. To get high-fidelity audio into your 2.1 speaker system, we use an **I2S DAC (Digital-to-Analog Converter)**.
I2S is a digital audio interface that sends uncompressed digital audio directly from the Pi's CPU to the DAC chip. The DAC converts it to a clean, line-level analog signal that your speakers' amplifier can process.

## 2. Equipment List

| Component | Description & Purpose |
|-----------|-----------------------|
| **Raspberry Pi Zero 2W** | The core brain handling Wi-Fi, AirPlay decoding, and driving the peripherals. |
| **PCM5102A I2S DAC** | Highly recommended DAC for Raspberry Pi. Provides excellent audio quality and outputs a standard line-level signal via a 3.5mm jack or RCA pads. |
| **8x8 WS2812B LED Matrix** | A 64-LED matrix for the dynamic frequency audio visualizer (bass, mids, highs). |
| **WS2812B Single LED** | The status indicator for connection state. (Cut from a strip or use a standalone module). |
| **SPI TFT LCD Screen** | 2.8" ST7789 (240x320, vertical) for album art and UI. |
| **5D Rocker Joystick Module** | Digital 5-way joystick (Up, Down, Left, Right, Center) plus Set/Reset buttons for media controls. |
| **5V 4A+ Power Supply** | 64 WS2812B LEDs can draw up to ~3.8 Amps at full white brightness. Beefy supply prevents crashes from voltage drops. |
| **Misc** | MicroSD Card (16GB+), Jumper wires, breadboard/perfboard, 3.5mm audio cable. |

## 3. Hardware Pinout & Wiring Architecture

To avoid hardware resource conflicts, components are mapped to their dedicated hardware interfaces:
*   **I2S** for the DAC (GPIO 18, 19, 21).
*   **SPI0** for the WS2812 LEDs (GPIO 10 MOSI) — **NOT PWM**, because PWM shares hardware with I2S.
*   **Software SPI** for the TFT LCD (pins TBD based on chosen module — deferred).
*   **Standard GPIOs** for the 5D Rocker digital buttons.

> **⚠️ Why SPI instead of PWM for LEDs?**
> GPIO 12/18 (PWM0) shares the same hardware peripheral as the I2S audio bus.
> When the `hifiberry-dac` overlay is loaded for the DAC, PWM becomes unavailable.
> Using SPI0 MOSI (GPIO 10) avoids this conflict entirely.

### I2S DAC (PCM5102A) -> Audio Output
| DAC Pin | RPi Zero 2W Pin | Purpose |
|:---:|:---|:---|
| VIN | 5V (Pin 2) | Power |
| GND | GND (Pin 6) | Ground |
| BCK | GPIO 18 (Pin 12) | I2S Bit Clock |
| LCK | GPIO 19 (Pin 35) | I2S Word Select (Left/Right Clock) |
| DIN | GPIO 21 (Pin 40) | I2S Data In |
| SCK | GND | Connect to GND to enable internal PLL clock |

### TFT LCD Screen (2.8" ST7789 240x320) -> Album Art
Because hardware SPI0 is used by the WS2812 LED driver to avoid audio interference, and hardware SPI1 conflicts with the I2S DAC, we use a **Software SPI overlay (`spi-gpio`)** for the TFT. 

Add `dtoverlay=spi-gpio,sck_pin=5,mosi_pin=6,miso_pin=13,cs0_pin=25,spi_bus=1` to `/boot/firmware/config.txt` to create `/dev/spidev1.0`.

| TFT Pin | RPi Zero 2W Pin | Purpose |
|:---:|:---|:---|
| VCC | 3.3V (Pin 1) or 5V (Pin 4) | Power (check display rating) |
| GND | GND (Pin 9) | Ground |
| SCL / SCK | GPIO 5 (Pin 29) | SPI1 Clock |
| SDA / MOSI | GPIO 6 (Pin 31) | SPI1 Data In |
| RES / RST | GPIO 12 (Pin 32) | Reset |
| DC | GPIO 24 (Pin 18) | Data/Command |
| CS | GPIO 25 (Pin 22) | Chip Select (CS0) |
| BLK | GPIO 20 (Pin 38) | Backlight control |

### WS2812B LEDs -> Visualizer & Status
*Optimization Tip: Daisy-chain the status LED and the Matrix to use only ONE GPIO pin.*
**Data Path:** RPi GPIO 10 -> **Data IN** (Status LED) | **Data OUT** (Status LED) -> **Data IN** (8x8 Matrix).

| LED Pin | RPi Zero 2W Pin / Source | Purpose |
|:---:|:---|:---|
| 5V / VCC | 5V External Power Supply | **Do not power from Pi 5V pin!** See power notes below. |
| GND | GND (Pin 39) + External PSU | Common ground is required. |
| DIN | GPIO 10 (Pin 19) | SPI0 MOSI — drives WS2812 via SPI (avoids I2S/PWM conflict). |

### Media Control Joystick (5D Rocker Module)
This module acts as 7 separate digital buttons. The COM pin is usually connected to GND, and the direction pins go to standard GPIOs. We configure the RPi GPIOs with internal Pull-Up resistors in software.

*Assuming COM connects to GND (Active Low inputs):*
| Rocker Pin | RPi Zero 2W Pin | Mapped Function |
|:---:|:---|:---|
| COM | GND (e.g., Pin 14) | Common Ground |
| UP | GPIO 16 (Pin 36) | Volume Up |
| DOWN | GPIO 17 (Pin 11) | Volume Down |
| LEFT | GPIO 22 (Pin 15) | Previous Track |
| RIGHT | GPIO 23 (Pin 16) | Next Track |
| MID (Press) | GPIO 26 (Pin 37) | Play / Pause |
| SET | GPIO 27 (Pin 13) | Cycle Visualizer Modes |
| RST | GPIO 4 (Pin 7) | Toggle AirPlay / Bluetooth (hold 1 s) |

## 4. Crucial Power Distribution Note
Do **NOT** power the 8x8 LED matrix directly from the Raspberry Pi's 5V pins. If the matrix lights up fully, it will pull too much current and burn out the Pi's power traces or cause instant reboots.

**Correct Power Wiring:**
1. Use a standard high-capacity 5V 4A or 5V 5A power supply.
2. Split the 5V power line in parallel:
   - One 5V branch goes to the Raspberry Pi to power it.
   - The other 5V branch goes directly to the VCC of the LED matrix.
3. Connect all Grounds together (Power Supply GND, Pi GND, Matrix GND) so the data signals have a common reference.

## 5. Audio Routing Pipeline

```
shairport-sync (or bluealsa-aplay)
     ↓  writes to
hw:Loopback,0,0   (ALSA loopback, playback side)
     ↕  kernel couples playback ↔ capture
hw:Loopback,1,0   (ALSA loopback, capture side)
     ↓  shared via dsnoop
     ├── CAVA           → frequency analysis → LED matrix
     └── alsaloop       → softvol → hw:DAC → speakers
```

Both AirPlay and Bluetooth feed into the same loopback bus. Switching modes just swaps which source writes to `hw:Loopback,0,0`. The downstream pipeline stays running.
