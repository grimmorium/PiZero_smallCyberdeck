"""2.4 GHz spectrum analyzer based on the nRF24L01(+), for PyAppsRunner.

PyAppsRunner contract: a module-level run(ctx) function returning None/0 (success)
or an int (error code). Module import has no side effects (hardware libraries are
imported inside run() only).

Hardware configuration is read from the interfaces_config.xml file (next to the
script first, then in the application base directory), from the element:

    <spi_device name="NRF24" type="radio" bus="1" device="1" speed_hz="4000000" mode="0">
        <pin name="CE"  gpio="23"/>
        <pin name="IRQ" gpio="26"/>      <!-- not used by this script -->
    </spi_device>

Other devices in the file (RC522, CC1101, GPS...) are ignored.

Optional attributes of the <spi_device name="NRF24"> element (defaults when absent):
    gpio_chip    (0)    gpiochip number for lgpio
    data_rate    (2M)   250K | 1M | 2M
    channel_min  (0)    0..125
    channel_max  (125)  0..125
    dwell_us     (170)  130..2000, listen time per channel
    sweeps       (6)    1..100, number of passes averaged into one frame
    height       (12)   3..60, maximum chart height in rows

Required packages (Raspberry Pi OS Bookworm):
    sudo apt install python3-spidev python3-lgpio

Return codes: 0 - OK / Ctrl+C, 2 - configuration error, 3 - hardware/SPI/GPIO error,
4 - missing required library.
"""
from __future__ import annotations

import shutil
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

CFG_NAME = "interfaces_config.xml"
DEVICE_NAME = "NRF24"

# Rejestry nRF24L01
R_CONFIG, R_EN_AA, R_SETUP_AW, R_RF_CH, R_RF_SETUP, R_STATUS, R_RPD = (
    0x00, 0x01, 0x03, 0x05, 0x06, 0x07, 0x09)
CMD_W_REG = 0x20
CMD_FLUSH_RX = 0xE2
RF_SETUP_BY_RATE = {"250K": 0x26, "1M": 0x06, "2M": 0x0E}  # 0 dBm

BLOCKS = " ▁▂▃▄▅▆▇█"
RESET = "\x1b[0m"
GREEN, YELLOW, RED, DIM = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[2m"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    spi_bus: int
    spi_device: int
    spi_speed_hz: int
    spi_mode: int
    gpio_chip: int
    ce_pin: int
    ch_min: int
    ch_max: int
    dwell_us: int
    sweeps: int
    data_rate: str
    height: int


# --------------------------------------------------------------------------- XML

def _int(elem, attr, default, lo, hi):
    raw = elem.get(attr)
    if raw is None or raw.strip() == "":
        if default is None:
            raise ConfigError(f"<{elem.tag}> requires attribute '{attr}'")
        return default
    try:
        val = int(raw.strip(), 0)
    except ValueError:
        raise ConfigError(f"<{elem.tag} {attr}=\"{raw}\"> is not an integer")
    if not lo <= val <= hi:
        raise ConfigError(f"<{elem.tag} {attr}=\"{raw}\"> out of range {lo}..{hi}")
    return val


def _find_by_name(parent, tag, name):
    """First <tag name="..."> element (case-insensitive)."""
    for e in parent.findall(tag):
        if (e.get("name") or "").strip().upper() == name.upper():
            return e
    return None


def load_config(path: Path) -> Config:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise ConfigError(f"invalid XML: {e}")
    except OSError as e:
        raise ConfigError(f"cannot read the file: {e}")
    if root.tag != "interfaces":
        raise ConfigError(f"the root element must be <interfaces>, got <{root.tag}>")

    dev = _find_by_name(root, "spi_device", DEVICE_NAME)
    if dev is None:
        raise ConfigError(f"missing element <spi_device name=\"{DEVICE_NAME}\">")
    ce = _find_by_name(dev, "pin", "CE")
    if ce is None:
        raise ConfigError(f"<spi_device name=\"{DEVICE_NAME}\"> requires the element "
                          "<pin name=\"CE\" gpio=\"...\"/>")

    rate = (dev.get("data_rate") or "2M").strip().upper()
    if rate not in RF_SETUP_BY_RATE:
        raise ConfigError(f"<spi_device data_rate=\"{rate}\"> - allowed values: 250K, 1M, 2M")

    cfg = Config(
        spi_bus=_int(dev, "bus", None, 0, 6),
        spi_device=_int(dev, "device", None, 0, 2),
        spi_speed_hz=_int(dev, "speed_hz", 4_000_000, 100_000, 10_000_000),
        spi_mode=_int(dev, "mode", 0, 0, 3),
        gpio_chip=_int(dev, "gpio_chip", 0, 0, 10),
        ce_pin=_int(ce, "gpio", None, 0, 57),            # numeracja BCM
        ch_min=_int(dev, "channel_min", 0, 0, 125),
        ch_max=_int(dev, "channel_max", 125, 0, 125),
        dwell_us=_int(dev, "dwell_us", 170, 130, 2000),
        sweeps=_int(dev, "sweeps", 6, 1, 100),
        data_rate=rate,
        height=_int(dev, "height", 12, 3, 60),
    )
    if cfg.ch_min > cfg.ch_max:
        raise ConfigError("channel_min cannot be greater than channel_max")
    return cfg


def _find_cfg(ctx) -> Path:
    for d in (ctx.script_dir, ctx.base_dir):
        p = Path(d) / CFG_NAME
        if p.is_file():
            return p
    return Path(ctx.script_dir) / CFG_NAME  # only used in the error message


# ------------------------------------------------------------------------- radio

class Radio:
    def __init__(self, cfg: Config, spidev, lgpio):
        self.cfg, self._spidev, self._lgpio = cfg, spidev, lgpio
        self.spi = None
        self.h = None

    def open(self):
        c = self.cfg
        self.spi = self._spidev.SpiDev()
        self.spi.open(c.spi_bus, c.spi_device)
        self.spi.max_speed_hz = c.spi_speed_hz
        self.spi.mode = c.spi_mode
        self.h = self._lgpio.gpiochip_open(c.gpio_chip)
        self._lgpio.gpio_claim_output(self.h, c.ce_pin, 0)

        # presence test of the chip: write/read of the RF_CH register
        self.write(R_RF_CH, 0x2A)
        if self.read(R_RF_CH) != 0x2A:
            raise OSError("nRF24 not responding (check wiring, CSN, 3.3 V power supply)")

        self.write(R_EN_AA, 0x00)                      # no auto-ACK
        self.write(R_SETUP_AW, 0x03)
        self.write(R_RF_SETUP, RF_SETUP_BY_RATE[c.data_rate])
        self.write(R_CONFIG, 0x03)                     # PWR_UP | PRIM_RX
        time.sleep(0.005)

    def read(self, reg):
        return self.spi.xfer2([reg & 0x1F, 0xFF])[1]

    def write(self, reg, val):
        self.spi.xfer2([CMD_W_REG | (reg & 0x1F), val])

    def close(self):
        try:
            if self.h is not None:
                self._lgpio.gpio_write(self.h, self.cfg.ce_pin, 0)
            if self.spi is not None:
                self.write(R_CONFIG, 0x00)             # power down
        except Exception:
            pass
        finally:
            if self.spi is not None:
                self.spi.close()
            if self.h is not None:
                try:
                    self._lgpio.gpio_free(self.h, self.cfg.ce_pin)
                except Exception:
                    pass
                self._lgpio.gpiochip_close(self.h)

    def sweep(self, acc):
        """One pass over the channels; acc[i] += 1 when RPD=1 on channel ch_min+i."""
        c = self.cfg
        xfer = self.spi.xfer2
        gw, h, ce = self._lgpio.gpio_write, self.h, c.ce_pin
        clock = time.perf_counter_ns
        dwell = c.dwell_us * 1000
        for i, ch in enumerate(range(c.ch_min, c.ch_max + 1)):
            xfer([CMD_W_REG | R_RF_CH, ch])
            gw(h, ce, 1)
            end = clock() + dwell
            while clock() < end:
                pass
            gw(h, ce, 0)
            if xfer([R_RPD, 0xFF])[1] & 1:
                acc[i] += 1
        xfer([CMD_FLUSH_RX])
        xfer([CMD_W_REG | R_STATUS, 0x70])


# ----------------------------------------------------------------------- render

def render(vals, peaks, cfg: Config, cols: int, lines: int, info: str) -> str:
    n = len(vals)
    group = max(1, -(-n // max(10, cols)))             # ceil(n / cols)
    ncols = -(-n // group)
    v = [max(vals[i:i + group]) for i in range(0, n, group)]
    p = [max(peaks[i:i + group]) for i in range(0, n, group)]
    rows = min(cfg.height, max(3, lines - 4))
    lv = [round(x * rows * 8) for x in v]
    pk = [min(rows - 1, max(0, round(x * rows * 8) - 1) // 8) for x in p]
    colors = [GREEN if x < 0.34 else YELLOW if x < 0.67 else RED for x in v]

    out = [info[:cols].ljust(cols)]
    for r in range(rows - 1, -1, -1):
        row = []
        for i in range(ncols):
            cell = max(0, min(8, lv[i] - r * 8))
            if cell:
                row.append(colors[i] + BLOCKS[cell])
            elif p[i] > 0 and pk[i] == r:
                row.append(DIM + "─")
            else:
                row.append(RESET + " ")
        out.append("".join(row) + RESET)

    axis = [" "] * ncols
    for f in range(2400, 2526, 25):
        idx = (f - 2400 - cfg.ch_min) // group
        label = str(f)
        if 0 <= idx and idx + len(label) <= ncols:
            axis[idx:idx + len(label)] = label
    out.append("".join(axis))
    out.append(f"{2400 + cfg.ch_min} MHz".ljust(ncols - 9) + f"{2400 + cfg.ch_max} MHz"
               if ncols > 24 else "")
    return "\x1b[H" + "\x1b[K\n".join(out) + "\x1b[K\x1b[J"


# ------------------------------------------------------------------------- run

def run(ctx):
    cfg_path = _find_cfg(ctx)
    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        print(f"Configuration error ({cfg_path}): {e}", file=sys.stderr)
        return 2

    try:
        import spidev
        import lgpio
    except ImportError as e:
        print(f"Missing library: {e.name}. Install with: sudo apt install python3-spidev python3-lgpio",
              file=sys.stderr)
        return 4

    radio = Radio(cfg, spidev, lgpio)
    try:
        radio.open()
    except FileNotFoundError:
        print(f"No /dev/spidev{cfg.spi_bus}.{cfg.spi_device}. Enable SPI "
              "(raspi-config; for SPI1 add dtoverlay=spi1-3cs to /boot/firmware/config.txt "
              "and reboot).", file=sys.stderr)
        radio.close()
        return 3
    except Exception as e:  # PermissionError, lgpio error, chip not present
        print(f"Hardware error: {e}", file=sys.stderr)
        radio.close()
        return 3

    n = cfg.ch_max - cfg.ch_min + 1
    peaks = [0.0] * n
    print(f"Configuration: {cfg_path}")
    print(f"nRF24 spectrum analyzer (SPI{cfg.spi_bus}.{cfg.spi_device}, CE=GPIO{cfg.ce_pin}) "
          "- Ctrl+C quits.")
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")   # alternate screen, no cursor
    result = 0
    try:
        t_prev = time.perf_counter()
        fps = 0.0
        while True:
            acc = [0] * n
            for _ in range(cfg.sweeps):
                radio.sweep(acc)
            vals = [a / cfg.sweeps for a in acc]
            peaks = [max(v, pk - 0.02) for v, pk in zip(vals, peaks)]

            now = time.perf_counter()
            fps = 0.8 * fps + 0.2 * (cfg.sweeps / (now - t_prev))
            t_prev = now

            top = max(range(n), key=vals.__getitem__)
            info = (f"nRF24 {2400 + cfg.ch_min}-{2400 + cfg.ch_max} MHz | {fps:4.1f} passes/s | "
                    f"max: {2400 + cfg.ch_min + top} MHz ({vals[top] * 100:3.0f}%) | Ctrl+C=quit"
                    if vals[top] > 0 else
                    f"nRF24 {2400 + cfg.ch_min}-{2400 + cfg.ch_max} MHz | {fps:4.1f} passes/s | "
                    "quiet | Ctrl+C=quit")
            size = shutil.get_terminal_size((80, 24))
            sys.stdout.write(render(vals, peaks, cfg, size.columns, size.lines, info))
            sys.stdout.flush()
    except KeyboardInterrupt:
        result = 0
    except Exception as e:                              # SPI/GPIO error during operation
        print_err = f"Hardware error during measurement: {e}"
        result = 3
    finally:
        sys.stdout.write(RESET + "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        radio.close()
    if result == 0:
        print("Interrupted.")
    else:
        print(print_err, file=sys.stderr)
    return result