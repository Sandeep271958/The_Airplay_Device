# The AirPlay Device: Audio Receiver & Visualizer

A Raspberry Pi-based high-fidelity AirPlay / Bluetooth audio receiver, real-time audio visualizer, album art display, and hardware media controller.

* **AirPlay Name:** `Sandy's Airplay Device`
* **Hostname:** `sandy.local`
* **Default SSH Login:** `ssh sandypi@sandy.local`

---

## Hardware Platforms

| Platform | Role | Audio Output | Status |
| :--- | :--- | :--- | :--- |
| **Raspberry Pi 4 Model B (1GB)** | **Current Prototype** | Onboard 3.5mm Headphone Jack (`bcm2835 Headphones`) | 🟢 Active & Running |
| **Raspberry Pi Zero 2 W** | **Production Target** | PCM5102A I2S DAC (`hifiberry-dac` overlay) | 🟡 Target hardware |

The codebase and installation scripts auto-detect the board at runtime, preserving the I2S DAC pinout plan while allowing full prototyping via the Pi 4B's 3.5mm jack.

---

## Features

- **Seamless AirPlay Audio:** Streams bit-perfect audio via `shairport-sync` with dynamic metadata and album art extraction.
- **ALSA Loopback Pipeline:** Simultaneously feeds line audio to the speakers and a `dsnoop` capture channel for analysis with zero lag.
- **Real-Time LED Visualizer:** Driven by `cava` in FIFO mode, powering an 8x8 WS2812B LED matrix and status indicator over hardware SPI0 MOSI (GPIO 10) to avoid I2S/PWM audio resource conflicts.
- **2.8" SPI TFT Display (ST7789, 240×320):** Displays album art, track details, animated clock, and visualizer modes via bitbanged software SPI (`spi-gpio`).
- **5D Rocker Joystick Controller:** 7 hardware inputs for Volume, Track Skipping, Play/Pause, Visualizer Mode cycling, and AirPlay/Bluetooth toggling.

---

## Hardware Wiring Reference

Both the Raspberry Pi 4B and Raspberry Pi Zero 2W share the identical 40-pin GPIO header.

### 1. Cable Set 1: 2.8" ST7789 TFT Display (240×320)

| TFT Module Pin | Function | RPi GPIO | Physical Pin # | Wire Color |
| :--- | :--- | :--- | :---: | :---: |
| **VCC** | 3.3V Power *(or 5V if module has onboard LDO)* | 3V3 | **Pin 1** | **🔴 Red** |
| **GND** | Ground | GND | **Pin 6** | **🟤 Brown** |
| **CS** | Chip Select (Bus 1) | **GPIO 25** | **Pin 22** | **🟠 Orange** |
| **RESET** | Hardware Reset | **GPIO 12** | **Pin 32** | **🟡 Yellow** |
| **DC** | Data / Command | **GPIO 24** | **Pin 18** | **🟢 Green** |
| **SDI (MOSI)** | Serial Data In | **GPIO 6** | **Pin 31** | **🔵 Blue** |
| **SCK** | Serial Clock | **GPIO 5** | **Pin 29** | **🟣 Purple** |
| **LED** | Backlight Control | **GPIO 20** | **Pin 38** | **⚪ Grey** |
| *SDO (MISO)* | Display Read | — | *Unused* | — |
| *T_... / SD_...* | Touch & SD Card Pins | — | *Unused* | — |

### 2. Cable Set 2: 5D Rocker Joystick (Media Controls)

| Rocker Pin | Function | RPi GPIO | Physical Pin # | Wire Color |
| :--- | :--- | :--- | :---: | :---: |
| **COM** | Common Ground | GND | **Pin 14** | **⚫ Black** |
| **UP** | Volume Up | **GPIO 16** | **Pin 36** | **⚪ White** |
| **DOWN** | Volume Down | **GPIO 17** | **Pin 11** | **🔘 Grey** |
| **LEFT** | Previous Track | **GPIO 22** | **Pin 15** | **🟣 Purple** |
| **RIGHT** | Next Track | **GPIO 23** | **Pin 16** | **🔵 Blue** |
| **MID (Press)** | Play / Pause | **GPIO 26** | **Pin 37** | **🟢 Green** |
| **SET** | Cycle Display Modes | **GPIO 27** | **Pin 13** | **🟡 Yellow** |
| **RST** | Toggle AirPlay ↔ BT (hold 1s) | **GPIO 4** | **Pin 7** | **🟠 Orange** |

### 3. WS2812B LEDs (Matrix & Status)

| LED Pin | Connection | RPi GPIO | Physical Pin # | Notes |
| :--- | :--- | :--- | :---: | :--- |
| **5V / VCC** | External 5V Power Supply | — | — | **Do NOT power from Pi 5V pin!** |
| **GND** | Common Ground | GND | **Pin 39** | Must share ground with Pi |
| **DIN** | SPI0 MOSI | **GPIO 10** | **Pin 19** | Avoids PWM conflict |

### 4. Audio Output
* **Pi 4B Prototype:** Plug standard 3.5mm AUX audio cable directly into the onboard 3.5mm headphone jack.
* **Pi Zero 2 W Target:** PCM5102A DAC connected to GPIO 18 (BCK), GPIO 19 (LCK), GPIO 21 (DIN).

---

## Audio Routing Pipeline

```text
AirPlay / Bluetooth
       ↓
hw:Loopback,0,0 (ALSA Loopback Playback)
       ↕ (kernel coupled)
hw:Loopback,1,0 (ALSA Loopback Capture)
       ├─→ alsaloop → softvol_out → (RPi 3.5mm Jack OR PCM5102A DAC)
       └─→ CAVA (FIFO /tmp/cava.fifo) → LED Matrix Visualizer
```

---

## Installation & Setup

1. **Clone the repository on the Pi:**
   ```bash
   git clone https://github.com/Sandeep271958/The_Airplay_Device.git ~/Airplaydevice
   cd ~/Airplaydevice
   ```

2. **Run the installer:**
   ```bash
   bash install.sh
   ```
   *The installer automatically detects whether you are on a Pi 4B or Pi Zero 2W and installs the corresponding ALSA and boot configurations.*

3. **Configure `/boot/firmware/config.txt`:**
   For Pi 4B prototype, ensure the following lines are in `/boot/firmware/config.txt`:
   ```ini
   dtparam=audio=on
   # dtoverlay=hifiberry-dac
   dtparam=spi=on
   dtoverlay=spi-gpio,sck_pin=5,mosi_pin=6,miso_pin=13,cs0_pin=25,spi_bus=1
   hdmi_ignore_edid_audio=1
   ```

4. **Reboot:**
   ```bash
   sudo reboot
   ```

---

## System Services & Management

All components run as systemd services:

```bash
# Check service status
sudo systemctl status shairport-sync
sudo systemctl status alsaloop
sudo systemctl status airplaydevice

# View live application logs
sudo journalctl -u airplaydevice -f
```
