"""Skaner UART - nasłuch na liście prędkości (Raspberry Pi, GPIO14 = TXD, GPIO15 = RXD).

Skrypt zgodny z kontraktem PyAppsRunner: def run(ctx) -> int | None.
Import jest czysty (bez efektów ubocznych); pyserial ładowany jest dopiero w run().

Wymagania:  pip install pyserial
Port:       domyślnie /dev/serial0 (można wymusić zmienną środowiskową UART_PORT)
Format:     8N1
"""
import math
import os
import sys
import time

BAUD_RATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
DEFAULT_SECONDS = 10.0
PORT_CANDIDATES = ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0")


def _find_port():
    env = os.environ.get("UART_PORT")
    if env:
        return env
    for path in PORT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _ask_seconds():
    while True:
        raw = input(
            f"Czas nasłuchiwania na każdej prędkości w sekundach [{DEFAULT_SECONDS:g}]: "
        ).strip().replace(",", ".")
        if not raw:
            return DEFAULT_SECONDS
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
        print("Podaj liczbę dodatnią (np. 10).")


def _render(data):
    """Bajty -> tekst do wyświetlenia (znaki niedrukowalne jako \\xNN)."""
    out = []
    for b in data:
        if b == 10:
            out.append("\n")
        elif b == 13:
            continue
        elif b == 9 or 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f"\\x{b:02x}")
    return "".join(out)


def _printable_count(data):
    return sum(1 for b in data if b in (9, 10, 13) or 32 <= b < 127)


def _print_summary(stats):
    print("\n=== Podsumowanie ===")
    print(f"{'Prędkość':>9} | {'Bajty':>7} | {'Drukowalne':>10}")
    for baud, total, printable in stats:
        ratio = f"{100 * printable / total:.0f}%" if total else "-"
        print(f"{baud:>9} | {total:>7} | {ratio:>10}")
    candidates = [s for s in stats if s[1] > 0]
    if candidates:
        best = max(candidates, key=lambda s: (s[2] / s[1], s[1]))
        print(
            f"\nNajwięcej czytelnego tekstu: {best[0]} baud "
            "(heurystyka - dla protokołów binarnych ocena nie ma sensu)."
        )
    else:
        print("\nNie odebrano żadnych danych na żadnej z prędkości.")


def run(ctx):
    try:
        import serial  # pyserial
    except ImportError:
        print("Brak biblioteki pyserial. Zainstaluj: pip install pyserial", file=sys.stderr)
        return 1

    port = _find_port()
    if port is None:
        print(
            "Nie znaleziono portu UART. Włącz sprzętowy port szeregowy "
            "(raspi-config -> Interface Options -> Serial Port: "
            "login shell = Nie, serial hardware = Tak) lub ustaw UART_PORT.",
            file=sys.stderr,
        )
        return 1

    try:
        seconds = _ask_seconds()
    except (KeyboardInterrupt, EOFError):
        print("\nPrzerwano.")
        return 0

    print(f"Port: {port}, format 8N1, {seconds:g} s na prędkość.")
    print("Ctrl+C przerywa skanowanie.")

    stats = []
    try:
        ser = serial.Serial(port, BAUD_RATES[0], bytesize=8, parity="N", stopbits=1, timeout=0.1)
    except (serial.SerialException, OSError) as exc:
        print(f"Nie można otworzyć {port}: {exc}", file=sys.stderr)
        print("Sprawdź uprawnienia (grupa 'dialout') i czy port nie jest zajęty.", file=sys.stderr)
        return 1

    try:
        for baud in BAUD_RATES:
            print(f"\n=== {baud} baud ===")
            ser.baudrate = baud
            ser.reset_input_buffer()
            total = printable = 0
            end = time.monotonic() + seconds
            try:
                while time.monotonic() < end:
                    data = ser.read(ser.in_waiting or 1)
                    if not data:
                        continue
                    total += len(data)
                    printable += _printable_count(data)
                    sys.stdout.write(_render(data))
                    sys.stdout.flush()
            finally:
                stats.append((baud, total, printable))
                print(f"\n[{baud}: odebrano {total} B]")
    except KeyboardInterrupt:
        print("\nPrzerwano.")
    except (serial.SerialException, OSError) as exc:
        print(f"\nBłąd portu szeregowego: {exc}", file=sys.stderr)
        return 1
    finally:
        ser.close()

    _print_summary(stats)
    return 0