#!/usr/bin/env python3
"""
Test readout of a joystick from an Arduino over I2C (Raspberry Pi 3).

Frame from the Arduino (5 bytes):
  [0] X high, [1] X low, [2] Y high, [3] Y low, [4] button (1 = released, 0 = pressed)

Requirements:
  sudo apt install python3-smbus      (or: pip install smbus2)
  sudo raspi-config -> Interface Options -> I2C -> Enable

Run:
  python3 joystick_test.py
"""

import sys
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    sys.exit("smbus2 library not found. Install with: pip install smbus2")

I2C_BUS = 1          # on RPi 3 this is /dev/i2c-1 by default (pins 3 and 5)
I2C_ADDRESS = 0x28   # must match the Arduino side
FRAME_SIZE = 5
INTERVAL = 0.2       # seconds


def read_joystick(bus):
    """Reads a frame and returns (x, y, button_pressed)."""
    msg = i2c_msg.read(I2C_ADDRESS, FRAME_SIZE)
    bus.i2c_rdwr(msg)
    data = list(msg)

    x = (data[0] << 8) | data[1]
    y = (data[2] << 8) | data[3]
    pressed = data[4] == 0
    return x, y, pressed


def main():
    print(f"Reading from Arduino (I2C 0x{I2C_ADDRESS:02X}) every {INTERVAL} s. Press Ctrl+C to quit.\n")
    with SMBus(I2C_BUS) as bus:
        next_time = time.monotonic()
        while True:
            try:
                x, y, pressed = read_joystick(bus)
                state = "PRESSED" if pressed else "released"
                print(f"X: {x:4d}  Y: {y:4d}  Button: {state}")
            except OSError as e:
                print(f"I2C error: {e}")

            # fixed rate, no time drift
            next_time += INTERVAL
            time.sleep(max(0, next_time - time.monotonic()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nFinished.")
