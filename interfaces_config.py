"""Loading of interfaces_config.xml (SPI and UART devices). Standard library only."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = "interfaces_config.xml"


class ConfigError(Exception):
    """Error in the interface configuration file."""


@dataclass(frozen=True)
class SpiDevice:
    name: str
    bus: int                 # SPI bus number (1 = SPI1) -> /dev/spidev<bus>.<device>
    device: int              # chip-select (CE) number
    speed_hz: int = 1_000_000
    mode: int = 0
    type: str = ""
    pins: dict = field(default_factory=dict)   # e.g. {"RST": 22} (BCM numbering)

    @property
    def dev_path(self) -> str:
        return f"/dev/spidev{self.bus}.{self.device}"


@dataclass(frozen=True)
class UartDevice:
    name: str
    port: str
    baudrate: int
    timeout: float = 0.5
    type: str = ""


@dataclass
class InterfacesConfig:
    spi: dict    # name -> SpiDevice
    uart: dict   # name -> UartDevice

    def get_spi(self, name: str) -> SpiDevice:
        try:
            return self.spi[name]
        except KeyError:
            raise ConfigError(f"No SPI device '{name}' in the configuration") from None

    def get_uart(self, name: str) -> UartDevice:
        try:
            return self.uart[name]
        except KeyError:
            raise ConfigError(f"No UART device '{name}' in the configuration") from None


def _attr(el, name, conv=str, default=None, required=True):
    raw = el.get(name)
    if raw is None or raw.strip() == "":
        if required and default is None:
            raise ConfigError(f"<{el.tag}>: missing required attribute '{name}'")
        return default
    try:
        return conv(raw.strip())
    except ValueError:
        raise ConfigError(f"<{el.tag}>: invalid value for attribute {name}='{raw}'") from None


def load_config(path) -> InterfacesConfig:
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found: {path}") from None
    except ET.ParseError as e:
        raise ConfigError(f"XML syntax error in {path.name}: {e}") from None
    if root.tag != "interfaces":
        raise ConfigError("The root element must be named <interfaces>")

    spi, uart, used_pins, used_cs = {}, {}, {}, {}
    for el in root:
        if el.tag == "spi_device":
            name = _attr(el, "name")
            if name in spi:
                raise ConfigError(f"Duplicated SPI device name: {name}")
            pins = {}
            for p in el:
                if p.tag != "pin":
                    raise ConfigError(f"<spi_device {name}>: unknown element <{p.tag}>")
                pname, gpio = _attr(p, "name"), _attr(p, "gpio", int)
                if pname in pins:
                    raise ConfigError(f"{name}: duplicated pin '{pname}'")
                if gpio in used_pins:
                    raise ConfigError(f"GPIO{gpio} used twice: {used_pins[gpio]} and {name}.{pname}")
                used_pins[gpio] = f"{name}.{pname}"
                pins[pname] = gpio
            dev = SpiDevice(
                name=name, bus=_attr(el, "bus", int), device=_attr(el, "device", int),
                speed_hz=_attr(el, "speed_hz", int, 1_000_000, required=False),
                mode=_attr(el, "mode", int, 0, required=False),
                type=_attr(el, "type", str, "", required=False), pins=pins)
            key = (dev.bus, dev.device)
            if key in used_cs:
                raise ConfigError(f"{dev.dev_path} assigned twice: {used_cs[key]} and {name}")
            used_cs[key] = name
            spi[name] = dev
        elif el.tag == "uart_device":
            name = _attr(el, "name")
            if name in uart:
                raise ConfigError(f"Duplicated UART device name: {name}")
            uart[name] = UartDevice(
                name=name, port=_attr(el, "port"), baudrate=_attr(el, "baudrate", int),
                timeout=_attr(el, "timeout", float, 0.5, required=False),
                type=_attr(el, "type", str, "", required=False))
        else:
            raise ConfigError(f"Unknown element <{el.tag}> in the configuration")
    return InterfacesConfig(spi=spi, uart=uart)


def find_config(*dirs) -> Path:
    """Returns the first existing interfaces_config.xml from the given directories."""
    for d in dirs:
        p = Path(d) / CONFIG_FILENAME
        if p.is_file():
            return p
    raise ConfigError(f"{CONFIG_FILENAME} not found in: " + ", ".join(str(d) for d in dirs))
