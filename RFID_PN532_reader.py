"""RFID reader PN532 over I2C (Raspberry Pi Zero W) - script for PyAppsRunner.

PyAppsRunner contract: a module-level run(ctx) -> int | None function.
The import is clean (no side effects); the hardware is opened only inside run().

Standard library only: communication through /dev/i2c-N (os + fcntl),
without smbus / Adafruit CircuitPython.

After detecting a card, the script tries to read as much data from it as possible:
  * MIFARE Classic (Mini/1K/4K) - full sector dump, authentication with the default
    factory keys (FFFFFFFFFFFF, A0A1A2A3A4A5, D3F7D3F7D3F7, 000000000000);
    sectors protected by another key are skipped with a corresponding message;
  * MIFARE Ultralight / NTAG - readout of all memory pages plus an attempt to
    extract text stored as an NDEF "Text" record.

Requirements on the Raspberry Pi:
  * I2C enabled (sudo raspi-config -> Interface Options -> I2C),
  * user in the i2c group (sudo usermod -aG i2c $USER, then log in again),
  * PN532 module switched to I2C mode (switches/jumpers on the board).
Verification:  sudo i2cdetect -y 3   -> "24" should appear in the table (hex, i.e. 0x24).
"""
import os
import time
from collections import namedtuple

# ---------------------------------------------------------------- configuration
I2C_BUS = 3                # /dev/i2c-3 (pins GPIO5 = SDA, GPIO6 = SCL)
PN532_ADDRESS = 0x24       # i2cdetect shows addresses in hex: "24" = 0x24
POLL_PAUSE = 0.15          # pause between card polls [s]
ABSENT_POLLS_TO_RESET = 2  # this many empty polls = card removed
ULTRALIGHT_MAX_PAGES = 231  # with margin, covers NTAG213/215/216 and Ultralight

# ------------------------------------------------------------------- I2C constants
# I2C_SLAVE is the ioctl() request number defined in the Linux kernel header
# <linux/i2c-dev.h>. The /dev/i2c-N file itself does not "know" which chip on
# the bus we want to talk to - one bus serves many devices under different
# 7-bit addresses. The call:
#     fcntl.ioctl(fd, I2C_SLAVE, 0x24)
# tells the kernel driver: "for this file descriptor, all subsequent
# os.read()/os.write() calls must be directed to the device at address 0x24".
# From that point on, plain read/write on fd behave as if they were a conversation
# exclusively with the PN532 - there is no need to pass the address for every
# operation (unlike e.g. in the smbus library, where the address is given at each
# call). There is also I2C_SLAVE_FORCE (same purpose), which sets the address even
# if some kernel driver has already "claimed" it - not needed here.
I2C_SLAVE = 0x0703

# ------------------------------------------------------------ PN532 commands
CMD_GET_FIRMWARE_VERSION = 0x02
CMD_SAM_CONFIGURATION = 0x14
CMD_RF_CONFIGURATION = 0x32
CMD_IN_LIST_PASSIVE_TARGET = 0x4A
CMD_IN_DATA_EXCHANGE = 0x40

# commands sent to the card itself via InDataExchange
CARD_CMD_READ = 0x30
CARD_CMD_AUTH_KEY_A = 0x60
CARD_CMD_AUTH_KEY_B = 0x61

ACK_FRAME = b"\x00\x00\xff\x00\xff\x00"

# the most popular factory/default MIFARE Classic keys
DEFAULT_KEYS = (
    bytes.fromhex("FFFFFFFFFFFF"),  # factory key (most blank cards)
    bytes.fromhex("A0A1A2A3A4A5"),  # typical MAD / NDEF key (NFC Forum)
    bytes.fromhex("D3F7D3F7D3F7"),  # typical key B for NDEF sectors
    bytes.fromhex("000000000000"),  # sometimes seen "zeroed" key
)

Tag = namedtuple("Tag", "uid atqa sak tg")

SAK_NAMES = {
    0x00: "MIFARE Ultralight / NTAG",
    0x08: "MIFARE Classic 1K",
    0x09: "MIFARE Mini",
    0x10: "MIFARE Plus 2K",
    0x11: "MIFARE Plus 4K",
    0x18: "MIFARE Classic 4K",
    0x20: "ISO14443-4 (e.g. DESFire, contactless card)",
    0x28: "ISO14443-4 + MIFARE Classic 1K (e.g. SmartMX/JCOP)",
    0x88: "MIFARE Classic 1K (SAK variant)",
}


class PN532Error(Exception):
    """Communication error with the PN532."""


class PN532I2C:
    """Minimal PN532 driver over I2C (normal frames, ISO14443A mode)."""

    def __init__(self, bus, address):
        import fcntl  # local import: the module stays importable on Windows too

        self._fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
        try:
            # see the comment at the I2C_SLAVE constant above
            fcntl.ioctl(self._fd, I2C_SLAVE, address)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    # ------------------------------------------------------------ low level
    def _write(self, data):
        last_error = None
        for _ in range(10):  # the PN532 may NACK when it is busy
            try:
                os.write(self._fd, data)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.02)
        raise PN532Error("I2C write failed: %s" % last_error)

    def _read_ready_flag(self):
        try:
            raw = os.read(self._fd, 1)
        except OSError:
            return False
        return bool(raw) and bool(raw[0] & 0x01)

    def _wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self._read_ready_flag():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def _read_frame(self, size):
        """Reads the status byte + size bytes; returns only the data (without the status)."""
        try:
            raw = os.read(self._fd, size + 1)
        except OSError as exc:
            raise PN532Error("I2C read failed: %s" % exc)
        if not raw or not (raw[0] & 0x01):
            raise PN532Error("The module is not ready to be read")
        return raw[1:]

    # ------------------------------------------------------------ frames
    @staticmethod
    def _build_frame(command, params):
        data = bytes([0xD4, command]) + bytes(params)
        length = len(data)
        return (
            bytes([0x00, 0x00, 0xFF, length, (-length) & 0xFF])
            + data
            + bytes([(-sum(data)) & 0xFF, 0x00])
        )

    @staticmethod
    def _parse_response(raw, command):
        start = raw.find(b"\x00\x00\xff")
        if start < 0 or len(raw) < start + 6:
            raise PN532Error("Malformed response frame: %s" % raw.hex())
        length = raw[start + 3]
        if (length + raw[start + 4]) & 0xFF:
            raise PN532Error("Bad checksum of the frame length")
        if length == 1:
            raise PN532Error("The PN532 reported an application error (error frame)")
        end = start + 5 + length
        if len(raw) < end + 1:
            raise PN532Error("Response frame truncated")
        data = raw[start + 5:end]
        if (sum(data) + raw[end]) & 0xFF:
            raise PN532Error("Bad checksum of the data")
        if data[0] != 0xD5 or data[1] != command + 1:
            raise PN532Error("Unexpected response: %s" % data.hex())
        return bytes(data[2:])

    def call(self, command, params=b"", timeout=1.0, response_size=32):
        """Sends a command, checks the ACK and returns the response data (without D5 xx)."""
        self._write(self._build_frame(command, params))
        if not self._wait_ready(0.5):
            raise PN532Error("No ACK from the PN532 (check wiring and I2C mode)")
        ack = self._read_frame(len(ACK_FRAME))
        if ack != ACK_FRAME:
            raise PN532Error("Invalid ACK: %s" % ack.hex())
        if not self._wait_ready(timeout):
            raise PN532Error("Timed out waiting for a response")
        return self._parse_response(self._read_frame(response_size), command)

    # ------------------------------------------------------------ high level
    def firmware_version(self):
        data = self.call(CMD_GET_FIRMWARE_VERSION)
        if len(data) < 4:
            raise PN532Error("GetFirmwareVersion response too short")
        return data[0], data[1], data[2], data[3]  # IC, Ver, Rev, Support

    def sam_configuration(self):
        # normal mode, 1 s timeout (0x14 * 50 ms), IRQ line enabled
        self.call(CMD_SAM_CONFIGURATION, b"\x01\x14\x01")

    def set_passive_activation_retries(self, retries):
        # the default is 0xFF = wait forever; a finite retry count makes
        # InListPassiveTarget return when no card is present
        self.call(CMD_RF_CONFIGURATION, bytes([0x05, 0xFF, 0x01, retries]))

    def read_passive_target(self, timeout=1.0):
        """Returns a Tag (ISO14443A 106 kbps), or None when no card is in the field."""
        data = self.call(
            CMD_IN_LIST_PASSIVE_TARGET, b"\x01\x00", timeout=timeout, response_size=40
        )
        if not data or data[0] == 0:
            return None
        if len(data) < 6:
            raise PN532Error("InListPassiveTarget response too short")
        tg = data[1]
        uid_len = data[5]
        uid = bytes(data[6:6 + uid_len])
        if len(uid) != uid_len:
            raise PN532Error("Truncated UID")
        return Tag(uid=uid, atqa=bytes(data[2:4]), sak=data[4], tg=tg)

    # -------------------------------------------------- InDataExchange (card)
    def mifare_authenticate(self, tg, block, uid, key, key_type=CARD_CMD_AUTH_KEY_A):
        params = bytes([tg, key_type, block]) + key + uid[-4:]
        data = self.call(CMD_IN_DATA_EXCHANGE, params, response_size=16)
        return bool(data) and data[0] == 0x00

    def mifare_read_block(self, tg, block):
        data = self.call(
            CMD_IN_DATA_EXCHANGE, bytes([tg, CARD_CMD_READ, block]), response_size=20
        )
        if not data or data[0] != 0x00 or len(data) < 17:
            return None
        return data[1:17]


def format_uid(uid):
    return ":".join("%02X" % b for b in uid)


# ---------------------------------------------------------- MIFARE Classic

def classic_sector_layout(sak):
    """Returns a list of (sector_number, blocks_per_sector), or None when the
    SAK does not match a known MIFARE Classic layout."""
    if sak == 0x09:                 # Mini, 0.3K
        return [(s, 4) for s in range(5)]
    if sak in (0x08, 0x88):         # Classic 1K
        return [(s, 4) for s in range(16)]
    if sak == 0x18:                 # Classic 4K
        return [(s, 4) for s in range(32)] + [(s, 16) for s in range(32, 40)]
    return None


def try_authenticate_sector(reader, tg, trailer_block, uid):
    """Tries the default keys (A first, then B) on the given sector."""
    for key_type in (CARD_CMD_AUTH_KEY_A, CARD_CMD_AUTH_KEY_B):
        for key in DEFAULT_KEYS:
            if reader.mifare_authenticate(tg, trailer_block, uid, key, key_type):
                return key_type, key
    return None, None


def dump_mifare_classic(reader, tag):
    """Reads all accessible sectors with the default keys and prints the contents
    block by block. Returns a dict {block_number: 16 bytes}."""
    layout = classic_sector_layout(tag.sak)
    if layout is None:
        print("  Unknown sector layout for SAK 0x%02X - skipping the full readout." % tag.sak)
        return {}

    print("  Full dump of MIFARE Classic (%d sectors, default keys)..." % len(layout))
    dump = {}
    abs_block = 0
    for sector_index, blocks_in_sector in layout:
        trailer_block = abs_block + blocks_in_sector - 1
        key_type, key = try_authenticate_sector(reader, tag.tg, trailer_block, tag.uid)
        if key is None:
            print("  Sector %2d: no access (none of the default keys matched)" % sector_index)
            abs_block += blocks_in_sector
            continue

        key_label = "A" if key_type == CARD_CMD_AUTH_KEY_A else "B"
        print("  Sector %2d (key %s = %s):" % (sector_index, key_label, key.hex().upper()))
        for b in range(blocks_in_sector):
            block_num = abs_block + b
            data = reader.mifare_read_block(tag.tg, block_num)
            tag_label = " [trailer]" if b == blocks_in_sector - 1 else ""
            if data is None:
                print("    Block %3d%s: <read error>" % (block_num, tag_label))
                continue
            dump[block_num] = data
            print("    Block %3d%s: %s" % (block_num, tag_label, data.hex().upper()))
        abs_block += blocks_in_sector
    return dump


# ----------------------------------------------- MIFARE Ultralight / NTAG

def dump_ultralight(reader, tag, max_pages=ULTRALIGHT_MAX_PAGES):
    """Reads 4 pages (16 bytes) per READ command until an error/limit is hit.
    Returns a dict {page_number: 4 bytes}."""
    print("  Reading the Ultralight/NTAG memory (pages of 4 bytes)...")
    pages = {}
    page = 0
    while page < max_pages:
        data = reader.call(
            CMD_IN_DATA_EXCHANGE,
            bytes([tag.tg, CARD_CMD_READ, page]),
            response_size=20,
        )
        if not data or data[0] != 0x00 or len(data) < 17:
            # the first error usually means the end of user memory - stop reading
            break
        chunk = data[1:17]
        for i in range(4):
            p = page + i
            if p >= max_pages:
                break
            page_bytes = chunk[i * 4:(i + 1) * 4]
            pages[p] = page_bytes
            print("    Page %3d: %s" % (p, page_bytes.hex().upper()))
        page += 4

    text = extract_ndef_text(pages)
    if text is not None:
        print("  --> NDEF text read: %r" % text)
    return pages


def extract_ndef_text(pages):
    """A simple attempt to find an NDEF 'Text' record in the user data
    (pages from 4 onwards). Returns the text or None."""
    if not pages:
        return None
    max_page = max(pages)
    buf = bytearray()
    for p in range(4, max_page + 1):
        buf += pages.get(p, b"\x00\x00\x00\x00")

    i = 0
    while i + 1 < len(buf):
        tlv_type = buf[i]
        if tlv_type == 0x00:      # NULL TLV - padding
            i += 1
            continue
        if tlv_type == 0xFE:      # end-of-memory marker for NDEF
            break
        length = buf[i + 1]
        value_start = i + 2
        if length == 0xFF:        # long form (rare on small tags)
            if i + 3 >= len(buf):
                break
            length = (buf[i + 2] << 8) | buf[i + 3]
            value_start = i + 4
        if tlv_type == 0x03:      # NDEF message
            text = parse_ndef_text_record(bytes(buf[value_start:value_start + length]))
            if text is not None:
                return text
        i = value_start + length
    return None


def parse_ndef_text_record(ndef):
    """Unpacks the first short NDEF record of type 'T' (Text)."""
    if len(ndef) < 3:
        return None
    header = ndef[0]
    type_length = ndef[1]
    if header & 0x10:  # bit SR (Short Record)
        payload_length = ndef[2]
        type_start = 3
    else:
        if len(ndef) < 6:
            return None
        payload_length = int.from_bytes(ndef[2:6], "big")
        type_start = 6
    record_type = ndef[type_start:type_start + type_length]
    payload_start = type_start + type_length
    payload = ndef[payload_start:payload_start + payload_length]
    if record_type != b"T" or not payload:
        return None
    lang_len = payload[0] & 0x3F
    text_bytes = payload[1 + lang_len:]
    try:
        return text_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return text_bytes.decode("latin-1", errors="replace")


def dump_tag_data(reader, tag):
    """Picks the right readout procedure based on the SAK."""
    try:
        if tag.sak == 0x00:
            dump_ultralight(reader, tag)
        elif tag.sak in (0x08, 0x09, 0x18, 0x88):
            dump_mifare_classic(reader, tag)
        else:
            print("  Full data readout is not supported for this card type (SAK 0x%02X)." % tag.sak)
    except PN532Error as exc:
        print("  Data readout aborted: %s" % exc)


def run(ctx):
    print("=== RFID reader PN532 (I2C, bus %d, address 0x%02X) ===" % (I2C_BUS, PN532_ADDRESS))

    try:
        reader = PN532I2C(I2C_BUS, PN532_ADDRESS)
    except ImportError:
        print("This script requires Linux (fcntl module / /dev/i2c-N device).")
        return 1
    except FileNotFoundError:
        print("/dev/i2c-%d not found - enable I2C: sudo raspi-config -> Interface Options -> I2C." % I2C_BUS)
        return 1
    except PermissionError:
        print("No permission on /dev/i2c-%d - add the user to the i2c group:" % I2C_BUS)
        print("  sudo usermod -aG i2c $USER   (and log in again)")
        return 1
    except OSError as exc:
        print("Cannot open the I2C bus: %s" % exc)
        return 1

    try:
        ic, ver, rev, _support = reader.firmware_version()
        print("Detected PN5%02X, firmware %d.%d" % (ic, ver, rev))
        reader.sam_configuration()
        reader.set_passive_activation_retries(0x02)

        print("Hold a card/key fob to the reader. Ctrl+C quits.\n")
        last_uid = None
        absent = 0
        errors = 0
        while True:
            try:
                tag = reader.read_passive_target()
                errors = 0
            except PN532Error as exc:
                errors += 1
                if errors >= 5:
                    raise PN532Error("Too many consecutive errors, last one: %s" % exc)
                time.sleep(0.2)
                continue

            if tag is None:
                absent += 1
                if absent >= ABSENT_POLLS_TO_RESET and last_uid is not None:
                    print("Card removed.\n")
                    last_uid = None
            else:
                absent = 0
                if tag.uid != last_uid:
                    last_uid = tag.uid
                    kind = SAK_NAMES.get(tag.sak, "unknown type")
                    print("UID: %s  (%d B)" % (format_uid(tag.uid), len(tag.uid)))
                    print("  ATQA: %s  SAK: 0x%02X  -> %s" % (tag.atqa.hex().upper(), tag.sak, kind))
                    dump_tag_data(reader, tag)
                    print()
            time.sleep(POLL_PAUSE)

    except KeyboardInterrupt:
        print("\nReading stopped.")
        return 0
    except PN532Error as exc:
        print("PN532 error: %s" % exc)
        return 2
    finally:
        reader.close()