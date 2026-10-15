#!/usr/bin/env python3
"""
Sterowanie robotem joystickiem podłączonym przez Arduino (I2C) -> UDP.

Ramka z Arduino (5 bajtów):
  [0] X high, [1] X low, [2] Y high, [3] Y low, [4] przycisk (1 = puszczony, 0 = wciśnięty)

Do robota wysyłany jest datagram UDP w formacie "speed;direction" (tak samo jak w RoverGUI.py),
wartości -100..100 w krokach co 5, wysyłane co [network] send_interval sekund.

Konfiguracja (IP, port, kalibracja joysticka) jest czytana z pliku rover_config.ini
leżącego obok tego skryptu. Jeśli pliku nie ma, zostanie utworzony z wartościami domyślnymi.

Wymagania:
  sudo apt install python3-smbus      (lub: pip install smbus2)
  sudo raspi-config -> Interface Options -> I2C -> Enable
"""

import configparser
import math
import os
import socket
import sys
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    sys.exit("Brak biblioteki smbus2. Zainstaluj: pip install smbus2")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "rover_config.ini")
FRAME_SIZE = 5

DEFAULT_CONFIG = """\
[network]
ip = 192.168.1.74
port = 12000
# co ile sekund wysyłać dane do robota (w RoverGUI było 0.05)
send_interval = 0.05

[i2c]
# na RPi 3 domyślnie jest to /dev/i2c-1 (piny 3 i 5)
bus = 3
# musi się zgadzać z Arduino
address = 0x28

[joystick]
# wartość surowa odpowiadająca środkowi drążka i maksymalne wychylenie od środka
# (dla 10-bitowego ADC Arduino: 512 / 512)
center_x = 512
center_y = 512
range = 512
# martwa strefa w % wychylenia (eliminuje drgania wokół środka)
deadzone = 5
# odwrócenie osi, jeśli robot jedzie w złą stronę
invert_speed = false
invert_direction = false
# dzielnik ograniczający prędkość (1, 2, 5, 10 - jak przyciski w RoverGUI)
divider = 1
"""


class ConfigError(Exception):
    pass


class Config:
    pass


def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
        raise ConfigError(
            f"Nie znaleziono pliku konfiguracyjnego. Utworzono domyślny: {path}\n"
            f"Uzupełnij adres IP i port, a następnie uruchom ponownie."
        )

    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(path, encoding="utf-8")

    cfg = Config()
    try:
        cfg.ip = parser.get("network", "ip").strip()
        cfg.port = parser.getint("network", "port")
        cfg.send_interval = parser.getfloat("network", "send_interval", fallback=0.05)

        cfg.i2c_bus = parser.getint("i2c", "bus", fallback=3)
        cfg.i2c_address = int(parser.get("i2c", "address", fallback="0x28"), 0)

        cfg.center_x = parser.getint("joystick", "center_x", fallback=512)
        cfg.center_y = parser.getint("joystick", "center_y", fallback=512)
        cfg.range = parser.getint("joystick", "range", fallback=512)
        cfg.deadzone = parser.getfloat("joystick", "deadzone", fallback=5.0)
        cfg.invert_speed = parser.getboolean("joystick", "invert_speed", fallback=False)
        cfg.invert_direction = parser.getboolean("joystick", "invert_direction", fallback=False)
        cfg.divider = parser.getfloat("joystick", "divider", fallback=1)
    except (configparser.Error, ValueError) as e:
        raise ConfigError(f"Błąd w pliku konfiguracyjnym {path}: {e}")

    if not cfg.ip:
        raise ConfigError("Pole [network] ip nie może być puste.")
    if not (1 <= cfg.port <= 65535):
        raise ConfigError("Pole [network] port musi być w zakresie 1-65535.")
    if cfg.send_interval <= 0:
        raise ConfigError("Pole [network] send_interval musi być większe od 0.")
    if cfg.range <= 0 or cfg.divider <= 0:
        raise ConfigError("Pola [joystick] range i divider muszą być większe od 0.")

    return cfg


def read_joystick(bus, address):
    """Odczytuje ramkę i zwraca (x, y, przycisk_wciśnięty)."""
    msg = i2c_msg.read(address, FRAME_SIZE)
    bus.i2c_rdwr(msg)
    data = list(msg)

    x = (data[0] << 8) | data[1]
    y = (data[2] << 8) | data[3]
    pressed = data[4] == 0
    return x, y, pressed


def axis_to_percent(raw, center, rng, deadzone, invert):
    """Surowa wartość osi -> -100..100 (z martwą strefą)."""
    value = (raw - center) / rng * 100.0
    value = max(-100.0, min(100.0, value))
    if abs(value) < deadzone:
        value = 0.0
    return -value if invert else value


def to_command(x, y, cfg):
    """Przelicza odczyt joysticka na (speed, direction) - tak jak w RoverGUI."""
    raw_speed = axis_to_percent(y, cfg.center_y, cfg.range, cfg.deadzone, cfg.invert_speed)
    raw_direction = axis_to_percent(x, cfg.center_x, cfg.range, cfg.deadzone, cfg.invert_direction)

    # kwantyzacja do kroków co 5 i zastosowanie dzielnika (jak w TouchJoystickSquare)
    base_speed = round(raw_speed / 5.0) * 5.0
    base_direction = round(raw_direction / 5.0) * 5.0
    speed = int(base_speed / math.sqrt(cfg.divider))
    direction = int(base_direction / math.sqrt(cfg.divider))
    return speed, direction


def send_command(sock, cfg, speed, direction):
    payload = f"{speed};{direction}"
    try:
        sock.sendto(payload.encode("utf-8"), (cfg.ip, cfg.port))
    except OSError as e:
        print(f"[BŁĄD TRANSMISJI]: {e}")


def main():
    cfg = load_config()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[!] Transmisja UDP do {cfg.ip}:{cfg.port} co {cfg.send_interval} s "
          f"(I2C 0x{cfg.i2c_address:02X}). Ctrl+C aby zakończyć.\n")

    last = None
    try:
        with SMBus(cfg.i2c_bus) as bus:
            next_time = time.monotonic()
            while True:
                try:
                    x, y, pressed = read_joystick(bus, cfg.i2c_address)
                    speed, direction = to_command(x, y, cfg)
                except OSError as e:
                    # brak odczytu z joysticka = bezpieczniej zatrzymać robota
                    print(f"Błąd I2C: {e}")
                    speed, direction = 0, 0
                    x = y = pressed = None

                send_command(sock, cfg, speed, direction)

                if (speed, direction) != last:
                    last = (speed, direction)
                    print(f"SPEED: {speed:4} | DIRECTION: {direction:4} | "
                          f"DIVIDER: {cfg.divider:g} | raw X: {x} Y: {y}")

                # stała częstotliwość, bez dryfu czasu
                next_time += cfg.send_interval
                time.sleep(max(0, next_time - time.monotonic()))
    except KeyboardInterrupt:
        print("\nZakończono.")
    finally:
        # na koniec zatrzymaj robota (kilka razy - UDP nie gwarantuje dostarczenia)
        for _ in range(3):
            send_command(sock, cfg, 0, 0)
            time.sleep(0.02)
        sock.close()


def run(ctx):
    try:
        main()
    except ConfigError as e:
        print(f"[BŁĄD KONFIGURACJI] {e}")
    return None


#if __name__ == "__main__":
#    run(None)
