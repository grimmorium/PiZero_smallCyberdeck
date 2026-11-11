# Pi Zero Small Cyberdeck

A cyberdeck built around a **Raspberry Pi Zero W** that controls an RC rover. The joystick is read by an Arduino (I2C slave) and the commands are sent to the rover over UDP; the SPI LCD shows a live preview of the rover camera. Extra tools: GPS monitor, 2.4 GHz spectrum analyzer (nRF24L01+), RFID readers (MFRC522 + PN532) and hardware test scripts — all launched from a terminal menu ([PyAppsRunner](https://github.com/PyAppsRunner)).

## Hardware overview

| Component | Interface | Notes |
|---|---|---|
| ILI9488 SPI LCD (480×320, RGB565) | SPI0 + framebuffer `/dev/fb1` | camera preview via ffmpeg; setup from [ILI9488RPIScript_RaspberryPi_Zero](https://github.com/grimmorium/ILI9488RPIScript_RaspberryPi_Zero) |
| Arduino joystick (slave, I2C `0x28`) | custom I2C bus 3 on GPIO5/GPIO6 (`i2c-gpio` overlay → `/dev/i2c-3`) | sketch in [`joystick_raduino/`](joystick_raduino/joystick_raduino.ino) |
| MFRC522 RFID reader (SPI1.0, RST = GPIO22) | SPI1 (`dtoverlay=spi1-3cs`) | `RFID_read.py` |
| nRF24L01+ (SPI1.1, CE = GPIO23, IRQ = GPIO26) | SPI1 | spectrum analyzer: `nrf24_spectrum.py` |
| CC1101 (SPI1.2, GDO0 = GPIO27, GDO2 = GPIO12) | SPI1 | tested by `spi_devices_test.py` |
| PN532 RFID reader (I2C bus 3, address `0x24`) | `/dev/i2c-3` | MIFARE/Ultralight dump + NDEF text: `RFID_PN532_reader.py` |
| GPS NEO-6M (`/dev/serial0`, PL011 via `miniuart-bt`, 9600 baud) | UART, GPIO14 = TXD / GPIO15 = RXD | `gps_monitor.py` |

> SPI1 is used for the radio modules because SPI0 drives the LCD.
> All pin numbers in this project are **BCM** (GPIO) numbers, not physical pin numbers.

## Setup (Raspberry Pi OS Bookworm)

```bash
sudo raspi-config
```

- enable **SPI**,
- enable **I2C**,
- disable login via UART and enable the hardware serial/UART port.

Add this apt source to `/etc/apt/sources.list`:

```bash
deb [ arch=armhf ] http://mirror.netcologne.de/raspbian/raspbian/ bookworm main contrib non-free rpi
```

Clone the project and run the setup scripts:

```bash
# Update and install git
sudo apt update
sudo apt install -y git

sudo git clone https://github.com/grimmorium/PiZero_smallCyberdeck.git
cp PiZero_smallCyberdeck/refreshCyberdeck.sh refreshCyberdeck.sh
sudo chmod +x refreshCyberdeck.sh
sudo chmod +x PiZero_smallCyberdeck/config.sh

# Clone the LCD setup script repository and run it
git clone https://github.com/grimmorium/ILI9488RPIScript_RaspberryPi_Zero.git
chmod +x ILI9488RPIScript_RaspberryPi_Zero/setup_ili9488.sh
sudo ./ILI9488RPIScript_RaspberryPi_Zero/setup_ili9488.sh

# System configuration (i2c-tools, python3-smbus, tailscale, ...) and reboot
sudo ./PiZero_smallCyberdeck/config.sh
sudo reboot

# Update/refresh the system packages and reboot
sudo ./refreshCyberdeck.sh
sudo reboot
```

Final tweaks:

- remove `console=serial0,115200` from `/boot/firmware/cmdline.txt`, then reboot;
- add `i2c-dev` to `/etc/modules`, then reboot.

## Requirements (Python)

```bash
sudo apt install python3-smbus python3-spidev python3-lgpio ffmpeg i2c-tools
pip install smbus2 pyserial
```

Groups: the user needs access to I2C (`i2c` group), serial ports (`dialout`) and, for the camera preview, write access to `/dev/fb1` (`video` group).

## Files

| File | Description |
|---|---|
| [`config.txt`](config.txt) | Boot configuration: ILI9488 LCD (480×320 via `hdmi_cvt`), `enable_uart=1`, `core_freq=400`, overlays `miniuart-bt`, `spi1-3cs` and the custom I2C bus 3 (`i2c-gpio` on GPIO5/GPIO6). |
| [`cmdline.txt`](cmdline.txt) | Kernel command line (e.g. Wi-Fi regulatory domain `PL`). Remove `console=serial0,115200` so `/dev/serial0` is free for the GPS. |
| [`config.sh`](config.sh) | One-time system setup: installs i2c-tools, python3-smbus and tailscale; run with `sudo`. |
| [`refreshCyberdeck.sh`](refreshCyberdeck.sh) | System update/refresh script (apt upgrade + cleanup); run with `sudo`, then reboot. |
| [`network.sh`](network.sh) | Opens the network manager TUI (`sudo nmtui`) for configuring Wi-Fi. |
| [`runMenu.sh`](runMenu.sh) | Starts the PyAppsRunner terminal menu (`menu.xml`). |
| [`menu.xml`](menu.xml) | Menu definition: Joystick network / camera items and folders for Joystick, NRF24, GPS, RFID PN532 and Tools. |
| [`interfaces_config.xml`](interfaces_config.xml) | Hardware interface configuration shared by the scripts (SPI devices RC522/NRF24/CC1101 with pins, UART device for the GPS). See [Configuration](#configuration). |
| [`interfaces_config.py`](interfaces_config.py) | Loader/validator for `interfaces_config.xml` (used by the Python tools). |
| [`rover_config.ini`](rover_config.ini) | Rover control settings: rover IP/port, I2C bus/address, joystick calibration and camera preview. See [Configuration](#configuration). |
| [`joystick_net.py`](joystick_net.py) | Drives the rover from the Arduino joystick over UDP (`speed;direction`, −100..100 in steps of 5 — same protocol as RoverGUI). Prints current values to the console. |
| [`joystick_net_cam.py`](joystick_net_cam.py) | Same as above, plus a live preview of the rover camera (MJPEG over HTTP) on the SPI LCD: ffmpeg decodes and writes directly to `/dev/fb1` (RGB565). While the preview runs, console output is suppressed. |
| [`joystick_test.py`](joystick_test.py) | Raw joystick readout test from I2C bus 1 (`/dev/i2c-1`) — prints X/Y/button at a fixed rate. |
| [`joystick_menuItem.py`](joystick_menuItem.py) | Same test as above, but on the custom I2C bus 3 (`/dev/i2c-3`, GPIO5/GPIO6) and wrapped for PyAppsRunner. |
| [`gps_monitor.py`](gps_monitor.py) | GPS monitor (NEO-6M on `/dev/serial0` @9600): fix quality, coordinates, altitude, speed/course, satellite count, sentence/checksum statistics. |
| [`nrf24_spectrum.py`](nrf24_spectrum.py) | 2.4 GHz spectrum analyzer based on the nRF24L01(+): sweeps channels 0..125 and renders a live bar chart in the terminal (alternate screen). |
| [`spi_devices_test.py`](spi_devices_test.py) | SPI device test suite for RC522, NRF24, CC1101 and unknown devices: register write/read checks, resets, pin control; retries at 100 kHz to distinguish wiring faults from too-high clock rates. |
| [`UART_scanner.py`](UART_scanner.py) | UART scanner: listens on a list of baud rates (1200..115200), prints received bytes and a summary with the most "readable" rate — handy for finding an unknown device's speed. |
| [`RFID_read.py`](RFID_read.py) | MFRC522 reader over SPI using a built-in bare-metal driver (no external mfrc522 library): polls cards and prints UIDs. |
| [`RFID_PN532_reader.py`](RFID_PN532_reader.py) | PN532 reader over I2C (bus 3, address `0x24`), standard-library only: detects ISO14443A cards and dumps MIFARE Classic sectors (default keys) or Ultralight/NTAG pages, including NDEF "Text" extraction. |
| [`SPI_cfg.xml`](SPI_cfg.xml) | Legacy nRF24 configuration in the old `<spi_cfg>` format — kept for reference; `nrf24_spectrum.py` now reads `interfaces_config.xml`. |
| [`joystick_raduino/joystick_raduino.ino`](joystick_raduino/joystick_raduino.ino) | Arduino sketch: I2C slave at address `0x28`, continuously samples the joystick (A0/A1 + button on pin 2 with pull-up) and answers master requests with a 5-byte frame `[X hi, X lo, Y hi, Y lo, button]` (button: 1 = released, 0 = pressed). |

## Configuration

### `interfaces_config.xml`

Shared hardware description. The root element is `<interfaces>`; each device is an `<spi_device>` or a `<uart_device>`. Pin numbers are BCM GPIO numbers.

```xml
<interfaces>
  <spi_device name="RC522" type="rfid" bus="1" device="0" speed_hz="1000000" mode="0">
    <pin name="RST" gpio="22"/>
  </spi_device>

  <spi_device name="NRF24" type="radio" bus="1" device="1" speed_hz="4000000" mode="0">
    <pin name="CE"  gpio="23"/>
    <pin name="IRQ" gpio="26"/>      <!-- not used by nrf24_spectrum.py -->
  </spi_device>

  <spi_device name="CC1101" type="radio" bus="1" device="2" speed_hz="4000000" mode="0">
    <pin name="GDO0" gpio="27"/>
    <pin name="GDO2" gpio="12"/>
  </spi_device>

  <!-- GPIO14 (TXD) / GPIO15 (RXD); thanks to the miniuart-bt overlay,
       /dev/serial0 points to the PL011 -->
  <uart_device name="GPS" type="gps" port="/dev/serial0" baudrate="9600" timeout="0.5"/>
</interfaces>
```

- `bus`/`device`: SPI bus and chip-select (with `spi1-3cs`, device 0/1/2 = CE0 GPIO18 / CE1 GPIO17 / CE2 GPIO16).
- Optional attributes on `<spi_device name="NRF24">` for the spectrum analyzer: `data_rate` (`250K|1M|2M`), `channel_min`, `channel_max`, `dwell_us`, `sweeps`, `height`.
- An optional `gpio_chip` attribute (default 0) may be set on `<interfaces>`.

### `rover_config.ini`

Used by `joystick_net.py` / `joystick_net_cam.py`. If the file is missing, a default one is created.

```ini
[network]
ip = 192.168.1.74          # rover IP address
port = 12000               # UDP port (same as RoverGUI)
send_interval = 0.05       # seconds between datagrams

[i2c]
bus = 3                    # custom I2C bus on GPIO5/GPIO6 -> /dev/i2c-3
address = 0x28             # must match the Arduino sketch (I2C_ADDRESS)

[joystick]
center_x = 512             # raw value at stick center (10-bit ADC: 512)
center_y = 512
range = 512                # maximum deflection from center
deadzone = 5               # dead zone in % of deflection
invert_speed = true        # invert axis if the rover drives the wrong way
invert_direction = false
divider = 1                # speed limiter (1, 2, 5, 10 - like RoverGUI buttons)

[camera]                   # joystick_net_cam.py only
enabled = true
url = http://192.168.1.74:8081/stream   # rover MJPEG stream
framebuffer = /dev/fb1     # must be 16 bpp (RGB565)
```

### `config.txt` overlays

The boot configuration already contains the required overlays; for reference:

- `dtoverlay=spi1-3cs` — enables SPI1 with three chip-selects,
- `dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=5,i2c_gpio_scl=6` — custom I2C bus 3 for the Arduino joystick and the PN532,
- `dtoverlay=miniuart-bt` + `enable_uart=1` — frees `/dev/serial0` (PL011) from Bluetooth for the GPS.

## Usage

### Menu

```bash
./runMenu.sh
```

starts PyAppsRunner with [`menu.xml`](menu.xml): joystick network control, camera preview, joystick tests, nRF24 spectrum analyzer, GPS monitor, RFID readers and hardware tools. Every item runs one of the scripts above (each exposes a `run(ctx)` entry point returning an int error code).

### Direct execution

```bash
python3 joystick_net.py            # rover control from the joystick
python3 joystick_net_cam.py        # + camera preview on the LCD
python3 gps_monitor.py             # GPS monitor
python3 nrf24_spectrum.py          # 2.4 GHz spectrum analyzer (Ctrl+C quits)
python3 spi_devices_test.py        # test all SPI devices
python3 UART_scanner.py            # scan for an unknown UART device's baud rate
python3 RFID_read.py               # MFRC522 card reader
python3 RFID_PN532_reader.py       # PN532 card reader with data dump
```

All scripts stop on **Ctrl+C** and return to the shell/menu.

### Rover protocol

UDP datagrams in the format `speed;direction` (e.g. `40;-15`), values −100..100 quantized to steps of 5, sent every `send_interval` seconds — identical to [RoverGUI](https://github.com/rovergui). On exit the script sends several `0;0` datagrams so the rover stops even if a packet is lost.
