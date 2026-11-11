#!/usr/bin/env python3
"""
Rover control via a joystick connected through an Arduino (I2C) -> UDP,
plus a live view of the rover camera (MJPEG over HTTP) on the SPI display (framebuffer).

Frame from the Arduino (5 bytes):
  [0] X high, [1] X low, [2] Y high, [3] Y low, [4] button (1 = released, 0 = pressed)

A UDP datagram in the format "speed;direction" is sent to the rover (same as in RoverGUI.py);
values are -100..100 in steps of 5, sent every [network] send_interval seconds.

Configuration (IP, port, joystick calibration, camera) is read from the rover_config.ini file
located next to this script. If it does not exist, a default one is created.

Camera preview: ffmpeg decodes the stream and writes it directly to /dev/fb1.
While the preview is running, the script no longer prints current values to the console
(the text would overlap the image).

Requirements:
  sudo apt install python3-smbus ffmpeg      (or: pip install smbus2)
  sudo raspi-config -> Interface Options -> I2C -> Enable
  the user must be a member of the 'video' group (writing to /dev/fb1)
"""

import configparser
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    sys.exit("Brak biblioteki smbus2. Zainstaluj: pip install smbus2")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "rover_config.ini")
VIDEO_LOG_PATH = "/tmp/rover_video.log"
FRAME_SIZE = 5

DEFAULT_CONFIG = """\
[network]
ip = 192.168.1.74
port = 12000
# how often (in seconds) to send data to the rover (RoverGUI used 0.05)
send_interval = 0.05

[i2c]
# on RPi 3 this is /dev/i2c-1 by default (pins 3 and 5)
bus = 3
# must match the Arduino side
address = 0x28

[joystick]
# raw value corresponding to stick center and maximum deflection from center
# (for the 10-bit Arduino ADC: 512 / 512)
center_x = 512
center_y = 512
range = 512
# deadzone in % of deflection (removes jitter around center)
deadzone = 5
# invert axis if the rover drives the wrong way
invert_speed = false
invert_direction = false
# speed limiter divider (1, 2, 5, 10 - like the buttons in RoverGUI)
divider = 1

[camera]
# live view of the rover camera (MJPEG over HTTP) on the SPI display
enabled = true
url = http://192.168.1.74:8081/stream
framebuffer = /dev/fb1
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
            f"Configuration file not found. Created default: {path}\n"
            "Fill in the IP address and port, then run again."
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

        # the [camera] section is optional - an old ini file will use the defaults
        cfg.camera_enabled = parser.getboolean("camera", "enabled", fallback=True)
        cfg.camera_url = parser.get(
            "camera", "url", fallback=f"http://{cfg.ip}:8081/stream").strip()
        cfg.framebuffer = parser.get("camera", "framebuffer", fallback="/dev/fb1").strip()
    except (configparser.Error, ValueError) as e:
        raise ConfigError(f"Error in configuration file {path}: {e}")

    if not cfg.ip:
        raise ConfigError("[network] ip must not be empty.")
    if not (1 <= cfg.port <= 65535):
        raise ConfigError("[network] port must be in range 1-65535.")
    if cfg.send_interval <= 0:
        raise ConfigError("[network] send_interval must be greater than 0.")
    if cfg.range <= 0 or cfg.divider <= 0:
        raise ConfigError("[joystick] range and divider must be greater than 0.")

    return cfg


class VideoViewer:
    """
    Displays the video stream on the LCD framebuffer.

    Decoding and scaling are done by ffmpeg (C is much faster than Python on a Zero W).
    If ffmpeg exits (e.g., the rover is not running yet or Wi-Fi dropped),
    it is restarted after 2 seconds.
    """

    def __init__(self, url, fb_path):
        self.url = url
        self.fb_path = fb_path
        self._stop = threading.Event()
        self._proc = None
        self._thread = None
        self._log = None

    def _fb_size(self):
        base = f"/sys/class/graphics/{os.path.basename(self.fb_path)}"
        with open(f"{base}/virtual_size") as f:
            width, height = (int(v) for v in f.read().strip().split(","))
        with open(f"{base}/bits_per_pixel") as f:
            bpp = int(f.read().strip())
        return width, height, bpp

    def start(self):
        """Returns None on success, otherwise a string with the failure reason."""
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not found (sudo apt install ffmpeg)"
        try:
            width, height, bpp = self._fb_size()
        except (OSError, ValueError):
            return f"cannot read parameters of {self.fb_path}"
        if bpp != 16:
            return f"{self.fb_path} is {bpp} bpp, only 16 (RGB565) is supported"
        if not os.access(self.fb_path, os.W_OK):
            return f"no write permission on {self.fb_path} (user in group 'video'?)"

        # scale keeping the aspect ratio with black letterbox bars
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-i", self.url,
               "-vf", vf, "-pix_fmt", "rgb565le",
               "-f", "fbdev", self.fb_path]

        self._log = open(VIDEO_LOG_PATH, "w")
        self._thread = threading.Thread(target=self._run, args=(cmd,), daemon=True)
        self._thread.start()
        return None

    def _run(self, cmd):
        while not self._stop.is_set():
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=self._log)
            while not self._stop.is_set() and self._proc.poll() is None:
                self._stop.wait(0.2)
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._stop.wait(2.0)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._log:
            self._log.close()


def read_joystick(bus, address):
    """Reads a frame and returns (x, y, button_pressed)."""
    msg = i2c_msg.read(address, FRAME_SIZE)
    bus.i2c_rdwr(msg)
    data = list(msg)

    x = (data[0] << 8) | data[1]
    y = (data[2] << 8) | data[3]
    pressed = data[4] == 0
    return x, y, pressed


def axis_to_percent(raw, center, rng, deadzone, invert):
    """Raw axis value -> -100..100 (with a dead zone)."""
    value = (raw - center) / rng * 100.0
    value = max(-100.0, min(100.0, value))
    if abs(value) < deadzone:
        value = 0.0
    return -value if invert else value


def to_command(x, y, cfg):
    """Converts the joystick readout into (speed, direction) - same as in RoverGUI."""
    raw_speed = axis_to_percent(y, cfg.center_y, cfg.range, cfg.deadzone, cfg.invert_speed)
    raw_direction = axis_to_percent(x, cfg.center_x, cfg.range, cfg.deadzone, cfg.invert_direction)

    # quantize to steps of 5 and apply the divider (like in TouchJoystickSquare)
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
        print(f"[TRANSMISSION ERROR]: {e}")


def main():
    cfg = load_config()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[!] UDP transmission to {cfg.ip}:{cfg.port} every {cfg.send_interval} s "
          f"(I2C 0x{cfg.i2c_address:02X}). Press Ctrl+C to quit.\n")

    viewer = None
    if cfg.camera_enabled:
        viewer = VideoViewer(cfg.camera_url, cfg.framebuffer)
        error = viewer.start()
        if error:
            print(f"[!] Camera preview disabled: {error}\n")
            viewer = None
        else:
            print(f"[!] Camera preview: {cfg.camera_url} -> {cfg.framebuffer} "
                  f"(log: {VIDEO_LOG_PATH})")
            sys.stdout.write("\033[?25l")  # hide the console cursor
            sys.stdout.flush()

    # with the preview enabled we don't print current values - they would corrupt the image
    verbose = viewer is None

    last = None
    i2c_error_shown = False
    try:
        with SMBus(cfg.i2c_bus) as bus:
            next_time = time.monotonic()
            while True:
                try:
                    x, y, pressed = read_joystick(bus, cfg.i2c_address)
                    speed, direction = to_command(y, x, cfg)
                    i2c_error_shown = False
                except OSError as e:
                    # no joystick readout = safer to stop the rover
                    if not i2c_error_shown:
                        print(f"I2C error: {e}")
                        i2c_error_shown = True
                    speed, direction = 0, 0
                    x = y = pressed = None

                send_command(sock, cfg, speed, direction)

                if (speed, direction) != last:
                    last = (speed, direction)
                    if verbose:
                        print(f"SPEED: {speed:4} | DIRECTION: {direction:4} | "
                              f"DIVIDER: {cfg.divider:g} | raw X: {x} Y: {y}")

                # fixed rate, no time drift
                next_time += cfg.send_interval
                time.sleep(max(0, next_time - time.monotonic()))
    except KeyboardInterrupt:
        print("\nFinished.")
    finally:
        # stop the rover at exit (several times - UDP does not guarantee delivery)
        for _ in range(3):
            send_command(sock, cfg, 0, 0)
            time.sleep(0.02)
        sock.close()
        if viewer:
            viewer.stop()
            sys.stdout.write("\033[?25h")  # restore the cursor
            sys.stdout.flush()


def run(ctx):
    try:
        main()
    except ConfigError as e:
        print(f"[CONFIGURATION ERROR] {e}")
    return None


#if __name__ == "__main__":
#    run(None)
