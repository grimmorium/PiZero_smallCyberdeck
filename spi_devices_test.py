"""Test urządzeń SPI (RC522, NRF24, CC1101 i inne) dla PyAppsRunner.

Kontrakt PyAppsRunner: funkcja run(ctx) na poziomie modułu, zwraca None/0 (sukces)
lub int (błąd). Import modułu jest bez efektów ubocznych (spidev i lgpio są
importowane dopiero w run()).

Konfiguracja jest czytana z pliku interfaces_config.xml (najpierw obok skryptu,
potem w katalogu bazowym aplikacji). Testowane są wszystkie elementy <spi_device>;
elementy innego typu (np. <uart_device>) są pomijane.

Co jest sprawdzane (rozpoznanie po atrybucie name):
    RC522   reset sprzętowy (pin RST), VersionReg, zapis/odczyt rejestru,
            reset programowy
    NRF24   zapis/odczyt RF_CH i CONFIG, SETUP_AW, poziom pinu IRQ
    CC1101  reset, PARTNUM/VERSION, zapis/odczyt PKTLEN, reset przywraca
            wartości domyślne, sterowanie pinami GDO0/GDO2 (jeśli skonfigurowane)
    inne    tylko test aktywności linii MISO

Testy zmieniają rejestry układów tymczasowo (wartości są przywracane lub układ
jest resetowany). Jeśli test nie przechodzi przy skonfigurowanym speed_hz, skrypt
powtarza go przy 100 kHz, żeby odróżnić błąd okablowania od zbyt dużej prędkości.

Opcjonalny atrybut gpio_chip (domyślnie 0) może być na elemencie <interfaces>.

Wymagane pakiety (Raspberry Pi OS Bookworm):
    sudo apt install python3-spidev python3-lgpio
(lgpio jest opcjonalne: bez niego pomijane są tylko testy pinów RST/IRQ/GDO)

Kody zwracane: 0 - brak błędów (ostrzeżenia dozwolone) / Ctrl+C,
1 - co najmniej jedno urządzenie nie przeszło testu, 2 - błąd konfiguracji,
4 - brak biblioteki spidev.
"""
from __future__ import annotations

import shutil
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

CFG_NAME = "interfaces_config.xml"
SLOW_HZ = 100_000

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
RESET = "\x1b[0m"
COLORS = {OK: "\x1b[32m", WARN: "\x1b[33m", FAIL: "\x1b[31m", INFO: "\x1b[36m"}


class ConfigError(Exception):
    pass


@dataclass
class SpiDev:
    name: str
    kind: str
    bus: int
    device: int
    speed_hz: int
    mode: int
    pins: dict = field(default_factory=dict)   # NAZWA_WIELKIMI -> numer GPIO (BCM)


# --------------------------------------------------------------------------- XML

def _int(elem, attr, default, lo, hi):
    raw = elem.get(attr)
    if raw is None or raw.strip() == "":
        if default is None:
            raise ConfigError(f"<{elem.tag}> wymaga atrybutu '{attr}'")
        return default
    try:
        val = int(raw.strip(), 0)
    except ValueError:
        raise ConfigError(f"<{elem.tag} {attr}=\"{raw}\"> nie jest liczbą całkowitą")
    if not lo <= val <= hi:
        raise ConfigError(f"<{elem.tag} {attr}=\"{raw}\"> poza zakresem {lo}..{hi}")
    return val


def load_config(path: Path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise ConfigError(f"niepoprawny XML: {e}")
    except OSError as e:
        raise ConfigError(f"nie można odczytać pliku: {e}")
    if root.tag != "interfaces":
        raise ConfigError(f"element główny musi być <interfaces>, jest <{root.tag}>")

    devices = []
    for e in root.findall("spi_device"):
        name = (e.get("name") or "").strip()
        if not name:
            raise ConfigError("<spi_device> wymaga niepustego atrybutu 'name'")
        pins = {}
        for p in e.findall("pin"):
            pname = (p.get("name") or "").strip().upper()
            if not pname:
                raise ConfigError(f"<pin> w urządzeniu {name} wymaga atrybutu 'name'")
            pins[pname] = _int(p, "gpio", None, 0, 57)
        devices.append(SpiDev(
            name=name,
            kind=(e.get("type") or "").strip(),
            bus=_int(e, "bus", None, 0, 6),
            device=_int(e, "device", None, 0, 2),
            speed_hz=_int(e, "speed_hz", 1_000_000, 100_000, 10_000_000),
            mode=_int(e, "mode", 0, 0, 3),
            pins=pins,
        ))
    if not devices:
        raise ConfigError("brak elementów <spi_device>")

    if root.get("gpio_chip") is not None:
        chip = _int(root, "gpio_chip", 0, 0, 10)
    else:
        chip = 0
        for e in root.findall("spi_device"):
            if e.get("gpio_chip") is not None:
                chip = _int(e, "gpio_chip", 0, 0, 10)
                break
    return devices, chip


def _find_cfg(ctx) -> Path:
    for d in (ctx.script_dir, ctx.base_dir):
        p = Path(d) / CFG_NAME
        if p.is_file():
            return p
    return Path(ctx.script_dir) / CFG_NAME  # do komunikatu o błędzie


def check_config(devices):
    """Konflikty w konfiguracji: to samo SPI lub ten sam pin użyty dwa razy."""
    out, spi_seen, pin_seen = [], {}, {}
    for d in devices:
        key = (d.bus, d.device)
        if key in spi_seen:
            out.append((WARN, f"{d.name} i {spi_seen[key]} mają to samo SPI{d.bus}.{d.device}"))
        else:
            spi_seen[key] = d.name
        for pname, gpio in d.pins.items():
            if gpio in pin_seen:
                out.append((WARN, f"GPIO{gpio} użyty w {pin_seen[gpio]} i {d.name}.{pname}"))
            else:
                pin_seen[gpio] = f"{d.name}.{pname}"
    return out


# ------------------------------------------------------------------------- GPIO

class Gpio:
    """Cienka nakładka na lgpio: wyjścia zostają zajęte do końca testu."""

    def __init__(self, lgpio, handle):
        self.lg, self.h, self.claimed = lgpio, handle, set()

    def out(self, pin, level):
        if pin in self.claimed:
            self.lg.gpio_write(self.h, pin, level)
        else:
            self.lg.gpio_claim_output(self.h, pin, level)
            self.claimed.add(pin)

    def read(self, pin):
        if pin in self.claimed:
            return self.lg.gpio_read(self.h, pin)
        self.lg.gpio_claim_input(self.h, pin)
        try:
            return self.lg.gpio_read(self.h, pin)
        finally:
            self.lg.gpio_free(self.h, pin)

    def close(self):
        for pin in list(self.claimed):
            try:
                self.lg.gpio_free(self.h, pin)
            except Exception:
                pass
        try:
            self.lg.gpiochip_close(self.h)
        except Exception:
            pass


# ----------------------------------------------------------------------- sondy

def _hint(vals):
    if vals and all(v == 0x00 for v in vals):
        return " Same 0x00: brak zasilania lub MISO."
    if vals and all(v == 0xFF for v in vals):
        return " Same 0xFF: brak zasilania lub CSN."
    return ""


def _hex(vals):
    return ",".join(f"{v:02X}" for v in vals)


def probe_nrf24(spi, d, gpio, r):
    def rd(reg):
        return spi.xfer2([reg & 0x1F, 0xFF])[1]

    def wr(reg, val):
        spi.xfer2([0x20 | (reg & 0x1F), val])

    orig = rd(0x05)                                    # RF_CH
    seen = []
    for pat in (0x2A, 0x55):
        wr(0x05, pat)
        seen.append(rd(0x05))
    wr(0x05, orig)
    if seen != [0x2A, 0x55]:
        r.append((FAIL, f"RF_CH: wpisano 2A,55, odczyt {_hex(seen)}." + _hint(seen)))
        return
    r.append((OK, "RF_CH: zapis/odczyt"))

    orig_cfg = rd(0x00)                                # CONFIG
    wr(0x00, 0x03)
    back = rd(0x00) & 0x7F
    wr(0x00, orig_cfg)
    r.append((OK, "CONFIG: zapis/odczyt") if back == 0x03
              else (FAIL, f"CONFIG: wpisano 03, odczyt {back:02X}"))

    aw = rd(0x03)
    r.append((OK, f"SETUP_AW={aw:02X}") if aw & 0x03
              else (WARN, f"SETUP_AW={aw:02X} (oczekiwane 01..03)"))

    irq = d.pins.get("IRQ")
    if irq is None:
        r.append((INFO, "IRQ: brak w konfiguracji"))
    elif gpio is None:
        r.append((INFO, "IRQ: pominięto (brak lgpio)"))
    else:
        wr(0x07, 0x70)                                 # wyczyść flagi przerwań
        time.sleep(0.001)
        lvl = gpio.read(irq)
        r.append((OK, f"IRQ GPIO{irq}: stan wysoki") if lvl == 1
                 else (WARN, f"IRQ GPIO{irq}: stan niski, oczekiwany wysoki (sprawdź kabel)"))


def probe_rc522(spi, d, gpio, r):
    def rd(reg):
        return spi.xfer2([((reg << 1) & 0x7E) | 0x80, 0x00])[1]

    def wr(reg, val):
        spi.xfer2([(reg << 1) & 0x7E, val])

    rst = d.pins.get("RST")
    if rst is None:
        r.append((INFO, "RST: brak w konfiguracji"))
    elif gpio is None:
        r.append((INFO, "RST: pominięto (brak lgpio)"))
    else:
        gpio.out(rst, 0)
        time.sleep(0.02)
        gpio.out(rst, 1)
        time.sleep(0.06)
        r.append((OK, f"reset sprzętowy RST (GPIO{rst})"))

    ver = rd(0x37)                                     # VersionReg
    names = {0x91: "v1.0", 0x92: "v2.0", 0x88: "klon", 0xB2: "klon FM17522_1",
             0x12: "klon"}
    if ver in (0x00, 0xFF):
        r.append((FAIL, f"VersionReg={ver:02X}." + _hint([ver])))
        return
    if ver in names:
        r.append((OK, f"VersionReg={ver:02X} ({names[ver]})"))
    else:
        r.append((WARN, f"VersionReg={ver:02X} (nieznana wersja)"))

    wr(0x2D, 0xA5)                                     # TReloadRegL
    a = rd(0x2D)
    wr(0x2D, 0x5A)
    b = rd(0x2D)
    r.append((OK, "TReloadRegL: zapis/odczyt") if (a, b) == (0xA5, 0x5A)
             else (FAIL, f"TReloadRegL: wpisano A5,5A, odczyt {_hex([a, b])}"))

    wr(0x01, 0x0F)                                     # SoftReset
    time.sleep(0.06)
    after = rd(0x2D)
    r.append((OK, "reset programowy") if after == 0x00
             else (WARN, f"po resecie TReloadRegL={after:02X} (oczekiwane 00)"))


def probe_cc1101(spi, d, gpio, r):
    def strobe(cmd):
        spi.xfer2([cmd])

    def rd_status(reg):                                # rejestry 0x30..0x3D: bit burst
        return spi.xfer2([(reg & 0x3F) | 0xC0, 0x00])[1]

    def rd(reg):
        return spi.xfer2([(reg & 0x3F) | 0x80, 0x00])[1]

    def wr(reg, val):
        spi.xfer2([reg & 0x3F, val])

    def reset():
        strobe(0x30)                                   # SRES
        time.sleep(0.01)

    reset()
    part, ver = rd_status(0x30), rd_status(0x31)
    if ver in (0x00, 0xFF):
        r.append((FAIL, f"VERSION={ver:02X}, PARTNUM={part:02X}." + _hint([ver, part])))
        return
    r.append((OK, f"VERSION={ver:02X}") if ver == 0x14
             else (WARN, f"VERSION={ver:02X} (zwykle 14)"))
    r.append((OK, f"PARTNUM={part:02X}") if part == 0x00
             else (WARN, f"PARTNUM={part:02X} (oczekiwane 00)"))

    seen = []
    for pat in (0x5A, 0xA5):                           # PKTLEN
        wr(0x06, pat)
        seen.append(rd(0x06))
    r.append((OK, "PKTLEN: zapis/odczyt") if seen == [0x5A, 0xA5]
             else (FAIL, f"PKTLEN: wpisano 5A,A5, odczyt {_hex(seen)}"))

    reset()
    dflt = rd(0x06)
    r.append((OK, "SRES przywraca ustawienia") if dflt == 0xFF
             else (WARN, f"po SRES PKTLEN={dflt:02X} (oczekiwane FF)"))

    for pname, reg in (("GDO0", 0x02), ("GDO2", 0x00)):
        pin = d.pins.get(pname)
        if pin is None:
            r.append((INFO, f"{pname}: brak w konfiguracji"))
            continue
        if gpio is None:
            r.append((INFO, f"{pname}: pominięto (brak lgpio)"))
            continue
        levels = []
        for val in (0x2F, 0x6F):                       # HW=0, odwrócone => 1
            wr(reg, val)
            time.sleep(0.002)
            levels.append(gpio.read(pin))
        r.append((OK, f"{pname} GPIO{pin}: sterowanie 0/1") if levels == [0, 1]
                 else (FAIL, f"{pname} GPIO{pin}: odczyt {levels}, oczekiwane [0, 1] "
                             "(sprawdź kabel)"))
    reset()


def probe_generic(spi, d, gpio, r):
    data = spi.xfer2([0x00] * 4)
    if all(v == 0x00 for v in data) or all(v == 0xFF for v in data):
        r.append((WARN, f"nieznane urządzenie, MISO={_hex(data)} (brak odpowiedzi?)"))
    else:
        r.append((OK, f"nieznane urządzenie, MISO={_hex(data)} (są zmiany na linii)"))


def _run_probe(d, spi, gpio):
    r = []
    key = d.name.upper()
    try:
        if "RC522" in key:
            probe_rc522(spi, d, gpio, r)
        elif "NRF24" in key:
            probe_nrf24(spi, d, gpio, r)
        elif "CC1101" in key:
            probe_cc1101(spi, d, gpio, r)
        else:
            probe_generic(spi, d, gpio, r)
    except Exception as e:                              # błąd SPI/GPIO w trakcie testu
        r.append((FAIL, f"wyjątek: {e}"))
    return r


def _has(results, status):
    return any(s == status for s, _ in results)


def test_device(d, gpio, spidev):
    spi = spidev.SpiDev()
    try:
        spi.open(d.bus, d.device)
    except FileNotFoundError:
        return [(FAIL, f"brak /dev/spidev{d.bus}.{d.device} (sprawdź dtoverlay SPI)")]
    except OSError as e:
        return [(FAIL, f"otwarcie SPI: {e}")]
    try:
        spi.mode = d.mode
        spi.max_speed_hz = d.speed_hz
        res = _run_probe(d, spi, gpio)
        if _has(res, FAIL) and d.speed_hz > SLOW_HZ:
            spi.max_speed_hz = SLOW_HZ
            slow = _run_probe(d, spi, gpio)
            if not _has(slow, FAIL):
                return ([(WARN, f"błąd przy {d.speed_hz} Hz, ale działa przy "
                                f"{SLOW_HZ} Hz: skróć kable lub zmniejsz speed_hz")] + slow)
            res.append((INFO, f"przy {SLOW_HZ} Hz też nie działa"))
        return res
    finally:
        spi.close()


# ------------------------------------------------------------------------- wyjście

def say(tag, text, width):
    prefix = f"[{tag:<4}] "
    lines = textwrap.wrap(text, max(10, width - len(prefix))) or [""]
    print(COLORS[tag] + prefix + RESET + lines[0])
    for line in lines[1:]:
        print(" " * len(prefix) + line)


def _worst(results):
    for s in (FAIL, WARN):
        if _has(results, s):
            return s
    return OK


# ------------------------------------------------------------------------- run

def run(ctx):
    cfg_path = _find_cfg(ctx)
    try:
        devices, chip = load_config(cfg_path)
    except ConfigError as e:
        print(f"Błąd konfiguracji ({cfg_path}): {e}", file=sys.stderr)
        return 2

    try:
        import spidev
    except ImportError:
        print("Brak biblioteki: spidev. Zainstaluj: sudo apt install python3-spidev "
              "python3-lgpio", file=sys.stderr)
        return 4
    try:
        import lgpio
    except ImportError:
        lgpio = None

    width = max(30, shutil.get_terminal_size((80, 24)).columns)
    print(f"Konfiguracja: {cfg_path}")
    print(f"Test urządzeń SPI: {len(devices)}")

    for res in check_config(devices):
        say(res[0], res[1], width)

    gpio = None
    if lgpio is None:
        say(WARN, "brak lgpio: pomijam testy pinów (sudo apt install python3-lgpio)", width)
    else:
        try:
            gpio = Gpio(lgpio, lgpio.gpiochip_open(chip))
        except Exception as e:
            say(WARN, f"gpiochip{chip}: {e}. Pomijam testy pinów", width)

    summary = []
    try:
        for d in devices:
            print()
            print(f"== {d.name} (SPI{d.bus}.{d.device}, {d.speed_hz // 1000} kHz) ==")
            results = test_device(d, gpio, spidev)
            for status, text in results:
                say(status, text, width)
            summary.append((d.name, _worst(results)))
    except KeyboardInterrupt:
        print("\nPrzerwano.")
        return 0
    finally:
        if gpio is not None:
            gpio.close()

    print()
    print("== Podsumowanie ==")
    for name, status in summary:
        say(status, name, width)
    failed = [n for n, s in summary if s == FAIL]
    if failed:
        print(f"Niesprawne: {', '.join(failed)}")
        return 1
    print("Brak błędów.")
    return 0
