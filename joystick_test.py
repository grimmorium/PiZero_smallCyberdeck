#!/usr/bin/env python3
"""
Testowy odczyt joysticka z Arduino po I2C (Raspberry Pi 3).

Ramka z Arduino (5 bajtów):
  [0] X high, [1] X low, [2] Y high, [3] Y low, [4] przycisk (1 = puszczony, 0 = wciśnięty)

Wymagania:
  sudo apt install python3-smbus      (lub: pip install smbus2)
  sudo raspi-config -> Interface Options -> I2C -> Enable

Uruchomienie:
  python3 joystick_test.py
"""

import sys
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    sys.exit("Brak biblioteki smbus2. Zainstaluj: pip install smbus2")

I2C_BUS = 1          # na RPi 3 jest to /dev/i2c-1 (piny 3 i 5)
I2C_ADDRESS = 0x28   # musi się zgadzać z Arduino
FRAME_SIZE = 5
INTERVAL = 0.2       # sekundy


def read_joystick(bus):
    """Odczytuje ramkę i zwraca (x, y, przycisk_wciśnięty)."""
    msg = i2c_msg.read(I2C_ADDRESS, FRAME_SIZE)
    bus.i2c_rdwr(msg)
    data = list(msg)

    x = (data[0] << 8) | data[1]
    y = (data[2] << 8) | data[3]
    pressed = data[4] == 0
    return x, y, pressed


def main():
    print(f"Odczyt z Arduino (I2C 0x{I2C_ADDRESS:02X}) co {INTERVAL} s. Ctrl+C aby zakończyć.\n")
    with SMBus(I2C_BUS) as bus:
        next_time = time.monotonic()
        while True:
            try:
                x, y, pressed = read_joystick(bus)
                state = "WCIŚNIĘTY" if pressed else "puszczony"
                print(f"X: {x:4d}  Y: {y:4d}  Przycisk: {state}")
            except OSError as e:
                print(f"Błąd I2C: {e}")

            # stała częstotliwość, bez dryfu czasu
            next_time += INTERVAL
            time.sleep(max(0, next_time - time.monotonic()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nZakończono.")
