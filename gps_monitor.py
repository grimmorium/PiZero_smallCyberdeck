"""Podgląd danych z modułu GPS NEO-6M przez UART (PyAppsRunner: def run(ctx)).

Parametry portu (port, baudrate) pochodzą z interfaces_config.xml (urządzenie UART "GPS").
Wymaga: pip install pyserial
Ctrl+C - powrót do menu.
"""
from __future__ import annotations

import shutil
import sys
import time
from collections import deque

from interfaces_config import ConfigError, find_config, load_config

GPS_NAME = "GPS"
REDRAW_INTERVAL = 0.5
NO_DATA_WARNING_AFTER = 3.0

FIX_QUALITY = {0: "brak", 1: "GPS", 2: "DGPS", 3: "PPS", 4: "RTK", 5: "RTK float", 6: "estymowana"}
FIX_MODE = {1: "brak", 2: "2D", 3: "3D"}


# ---------------------------------------------------------------- NMEA
def checksum_ok(line: str) -> bool:
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cs = line[1:].partition("*")
    try:
        expected = int(cs[:2], 16)
    except ValueError:
        return False
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return calc == expected


def _coord(value: str, hemi: str):
    if not value or not hemi:
        return None
    try:
        dot = value.index(".")
        deg, minutes = float(value[:dot - 2]), float(value[dot - 2:])
    except (ValueError, IndexError):
        return None
    res = deg + minutes / 60.0
    return -res if hemi in ("S", "W") else res


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fmt_time(t: str) -> str:
    return f"{t[0:2]}:{t[2:4]}:{t[4:6]} UTC" if len(t) >= 6 else "-"


def _fmt_date(d: str) -> str:
    return f"20{d[4:6]}-{d[2:4]}-{d[0:2]}" if len(d) == 6 else "-"


class GpsState:
    def __init__(self):
        self.time = self.date = ""
        self.valid = None            # RMC: A/V
        self.lat = self.lon = None
        self.alt = None
        self.geoid = None
        self.quality = None
        self.sats_used = None
        self.hdop = self.vdop = self.pdop = None
        self.fix_mode = None
        self.speed_kmh = None
        self.course = None
        self.sats_in_view = {}       # talker -> lista (prn, elev, azim, snr)
        self._gsv_build = {}
        self.sentence_counts = {}
        self.bad_checksums = 0
        self.last_raw = deque(maxlen=5)

    def feed(self, line: str) -> bool:
        """Przetwarza jedną linię NMEA. Zwraca True, gdy zdanie było poprawne."""
        line = line.strip()
        if not line:
            return False
        self.last_raw.append(line)
        if not checksum_ok(line):
            self.bad_checksums += 1
            return False
        body = line[1:].split("*")[0]
        f = body.split(",")
        talker, kind = f[0][:2], f[0][2:]
        self.sentence_counts[kind] = self.sentence_counts.get(kind, 0) + 1
        handler = getattr(self, f"_on_{kind}", None)
        if handler:
            try:
                handler(f, talker)
            except (IndexError, ValueError):
                pass
        return True

    def _on_GGA(self, f, talker):
        self.time = f[1] or self.time
        self.lat, self.lon = _coord(f[2], f[3]), _coord(f[4], f[5])
        self.quality, self.sats_used = _i(f[6]), _i(f[7])
        self.hdop, self.alt, self.geoid = _f(f[8]), _f(f[9]), _f(f[11])

    def _on_RMC(self, f, talker):
        self.time = f[1] or self.time
        self.valid = f[2] == "A"
        if self.valid:
            self.lat, self.lon = _coord(f[3], f[4]), _coord(f[5], f[6])
        knots = _f(f[7])
        self.speed_kmh = knots * 1.852 if knots is not None else None
        self.course = _f(f[8])
        self.date = f[9] or self.date

    def _on_GLL(self, f, talker):
        pass  # pozycja jest już z GGA/RMC; zdanie liczone tylko w statystykach

    def _on_VTG(self, f, talker):
        self.course = _f(f[1]) if _f(f[1]) is not None else self.course
        kmh = _f(f[7])
        if kmh is not None:
            self.speed_kmh = kmh

    def _on_GSA(self, f, talker):
        self.fix_mode = _i(f[2])
        self.pdop, self.hdop, self.vdop = _f(f[15]), _f(f[16]), _f(f[17])

    def _on_GSV(self, f, talker):
        total, num = int(f[1]), int(f[2])
        if num == 1:
            self._gsv_build[talker] = []
        sats = self._gsv_build.setdefault(talker, [])
        for i in range(4, len(f) - 3, 4):
            prn = f[i]
            if prn:
                sats.append((prn, _i(f[i + 1]), _i(f[i + 2]), _i(f[i + 3])))
        if num == total:
            self.sats_in_view[talker] = list(sats)

    def render(self, width: int, height: int, port_info: str, no_data: bool, elapsed: float) -> list:
        def v(x, fmt="{}", unit=""):
            return "-" if x is None else fmt.format(x) + unit

        if self.fix_mode in FIX_MODE:
            fix = f"{FIX_MODE[self.fix_mode]}"
        else:
            fix = "-"
        quality = FIX_QUALITY.get(self.quality, "-") if self.quality is not None else "-"
        status = "BRAK DANYCH" if no_data else ("FIX OK" if self.valid else "szukam satelitów...")
        total_view = sum(len(s) for s in self.sats_in_view.values())

        lines = [
            f"=== GPS NEO-6M  |  {port_info}  |  {status} ===",
            f"Czas:        {_fmt_time(self.time)}    Data: {_fmt_date(self.date)}",
            f"Fix:         {fix}    Jakość: {quality}    Ważny (RMC): "
            f"{'tak' if self.valid else 'nie' if self.valid is not None else '-'}",
            f"Szerokość:   {v(self.lat, '{:.6f}', '°')}",
            f"Długość:     {v(self.lon, '{:.6f}', '°')}",
            f"Wysokość:    {v(self.alt, '{:.1f}', ' m')}    Geoida: {v(self.geoid, '{:.1f}', ' m')}",
            f"Prędkość:    {v(self.speed_kmh, '{:.1f}', ' km/h')}    Kurs: {v(self.course, '{:.1f}', '°')}",
            f"Satelity:    użyte {v(self.sats_used)}, widoczne {total_view}",
            f"DOP:         HDOP {v(self.hdop)}  VDOP {v(self.vdop)}  PDOP {v(self.pdop)}",
            "",
            "Satelity w zasięgu (PRN elev azym SNR):",
        ]
        all_sats = [(t, s) for t, lst in sorted(self.sats_in_view.items()) for s in lst]
        sat_cells = [f"{t}{p:>3} {v(e):>3}° {v(a):>3}° {v(s):>3}dB" for t, (p, e, a, s) in all_sats]
        col_w = 24
        cols = max(1, width // col_w)
        room = max(0, height - len(lines) - 6)
        rows = [sat_cells[i:i + cols] for i in range(0, len(sat_cells), cols)][:room]
        for r in rows:
            lines.append("  " + "".join(c.ljust(col_w) for c in r))
        if not sat_cells:
            lines.append("  (brak)")
        counts = " ".join(f"{k}:{n}" for k, n in sorted(self.sentence_counts.items()))
        lines += ["", f"Zdania: {counts or '-'}   błędne sumy kontrolne: {self.bad_checksums}",
                  f"Czas pracy: {int(elapsed)} s   (Ctrl+C - powrót do menu)", "Ostatnie dane:"]
        lines += ["  " + l for l in self.last_raw]
        if no_data:
            lines += ["",
                      "! Brak danych z portu. Sprawdź: TXD GPS -> GPIO15 (pin 10), zasilanie,",
                      "  wyłączoną konsolę szeregową i obecność /dev/serial0 (ttyAMA0)."]
        return [l[:width] for l in lines]


# ---------------------------------------------------------------- uruchomienie
def run(ctx):
    try:
        import serial
    except ImportError:
        print("Brak biblioteki pyserial. Zainstaluj: pip install pyserial", file=sys.stderr)
        return 1

    try:
        cfg_path = find_config(ctx.script_dir, ctx.base_dir)
        uart = load_config(cfg_path).get_uart(GPS_NAME)
    except ConfigError as e:
        print(f"Błąd konfiguracji: {e}", file=sys.stderr)
        return 2

    print(f"Konfiguracja: {cfg_path}")
    print(f"Otwieram {uart.port} @ {uart.baudrate} bps...")
    try:
        ser = serial.Serial(uart.port, uart.baudrate, timeout=uart.timeout)
    except (serial.SerialException, OSError) as e:
        print(f"Nie można otworzyć portu {uart.port}: {e}", file=sys.stderr)
        print("Sprawdź uprawnienia (grupa dialout), enable_uart=1 i wyłączoną konsolę szeregową.",
              file=sys.stderr)
        return 1

    state = GpsState()
    start = time.monotonic()
    last_data = start
    last_draw = 0.0
    port_info = f"{uart.port} @ {uart.baudrate}"
    sys.stdout.write("\x1b[2J")
    try:
        while True:
            try:
                raw = ser.readline()
            except (serial.SerialException, OSError) as e:
                print(f"\nBłąd odczytu z portu: {e}", file=sys.stderr)
                return 1
            now = time.monotonic()
            if raw:
                last_data = now
                state.feed(raw.decode("ascii", errors="replace"))
            if now - last_draw >= REDRAW_INTERVAL:
                last_draw = now
                size = shutil.get_terminal_size((80, 24))
                lines = state.render(size.columns, size.lines, port_info,
                                     no_data=(now - last_data) > NO_DATA_WARNING_AFTER,
                                     elapsed=now - start)
                sys.stdout.write("\x1b[H" + "".join(l + "\x1b[K\n" for l in lines) + "\x1b[J")
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nPrzerwano.")
        return 0
    finally:
        ser.close()
