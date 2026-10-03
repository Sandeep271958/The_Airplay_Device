# Airplay Device: Audio Receiver & Visualizer

This project is a Raspberry Pi Zero 2W based AirPlay Audio Receiver and Visualizer. It routes audio via AirPlay to an I2S DAC for high-fidelity output and uses CAVA to drive a WS2812B LED matrix as an audio visualizer. It also supports media controls via a 5D Rocker Joystick and displays album art on an SPI TFT LCD Screen.

## Features
- **AirPlay Audio Streaming:** Uses `shairport-sync` to stream audio to the Raspberry Pi.
- **High-Fidelity Audio:** Outputs through an I2S DAC (PCM5102A) to bypass the lack of onboard analog audio on the Pi Zero 2W.
- **LED Audio Visualizer:** Uses a 64-LED WS2812B matrix and CAVA (Console-based Audio Visualizer for ALSA) to create dynamic frequency visualizations (bass, mids, highs).
- **TFT Display:** Displays album artwork via an SPI TFT LCD screen.
- **Hardware Controls:** Uses a 5D Rocker Joystick module for Play/Pause, Volume Up/Down, Next/Previous Track, and toggling visualizer modes.

## Hardware Architecture
Please refer to [hardware_architecture.md](hardware_architecture.md) for detailed wiring diagrams, pinouts, and power distribution notes. 

**Key Components:**
- Raspberry Pi Zero 2W
- PCM5102A I2S DAC
- 8x8 WS2812B LED Matrix
- SPI TFT LCD Screen
- 5D Rocker Joystick Module

## Audio Pipeline
Audio is routed using an ALSA loopback device to allow both playback through the DAC and capture for the CAVA visualizer simultaneously:
```
shairport-sync -> hw:Loopback -> dsnoop -> (alsaloop -> DAC) & (CAVA -> LED matrix)
```

## Installation & Setup
Run the `install.sh` script to configure the system services, dependencies, and ALSA configuration.

```bash
bash install.sh
```

Ensure you follow the wiring instructions exactly, especially concerning power distribution for the LED matrix (use an external 5V 4A+ supply).
