"""Czytnik RFID PN532 po I2C (Raspberry Pi Zero W) - skrypt dla PyAppsRunner.

Kontrakt PyAppsRunner: funkcja run(ctx) -> int | None na poziomie modułu.
Import jest czysty (żadnych efektów ubocznych); sprzęt otwierany jest dopiero w run().

Tylko biblioteka standardowa: komunikacja przez /dev/i2c-N (os + fcntl),
bez smbus / Adafruit CircuitPython.

Po wykryciu karty skrypt próbuje odczytać z niej jak najwięcej danych:
  * MIFARE Classic (Mini/1K/4K) - pełny zrzut sektorów, autoryzacja domyślnymi
    kluczami fabrycznymi (FFFFFFFFFFFF, A0A1A2A3A4A5, D3F7D3F7D3F7, 000000000000);
    sektory chronione innym kluczem zostaną pominięte z odpowiednim komunikatem;
  * MIFARE Ultralight / NTAG - odczyt wszystkich stron pamięci oraz próba
    wyłuskania z nich tekstu zapisanego jako rekord NDEF "Text".

Wymagania na Raspberry Pi:
  * włączony I2C (sudo raspi-config -> Interface Options -> I2C),
  * użytkownik w grupie i2c (sudo usermod -aG i2c $USER, potem ponowne logowanie),
  * moduł PN532 przełączony w tryb I2C (przełączniki/zworki na płytce).
Weryfikacja:  sudo i2cdetect -y 3   -> w tabeli powinno być "24" (hex, czyli 0x24).
"""
import os
import time
from collections import namedtuple

# ---------------------------------------------------------------- konfiguracja
I2C_BUS = 3                # /dev/i2c-3 (piny GPIO5 = SDA, GPIO6 = SCL)
PN532_ADDRESS = 0x24       # i2cdetect pokazuje adresy szesnastkowo: "24" = 0x24
POLL_PAUSE = 0.15          # przerwa między odpytaniami o kartę [s]
ABSENT_POLLS_TO_RESET = 2  # tyle pustych odpytań = karta zdjęta
ULTRALIGHT_MAX_PAGES = 231  # z zapasem pokrywa NTAG213/215/216 i Ultralight

# ------------------------------------------------------------------- stałe I2C
# I2C_SLAVE to numer żądania ioctl() zdefiniowany w nagłówku jądra Linuksa
# <linux/i2c-dev.h>. Plik /dev/i2c-N sam w sobie nie "wie", z którym układem
# na magistrali chcemy rozmawiać - jedna magistrala obsługuje wiele urządzeń
# pod różnymi adresami 7-bitowymi. Wywołanie:
#     fcntl.ioctl(fd, I2C_SLAVE, 0x24)
# mówi sterownikowi jądra: "dla tego deskryptora pliku wszystkie kolejne
# os.read()/os.write() mają być kierowane do urządzenia o adresie 0x24".
# Od tego momentu zwykłe read/write na fd działają tak, jakby były rozmową
# wyłącznie z PN532 - nie trzeba już przekazywać adresu przy każdej operacji
# (inaczej niż np. w bibliotece smbus, gdzie adres podaje się przy każdym
# wywołaniu). Istnieje też I2C_SLAVE_FORCE (ten sam cel), które ustawia adres
# nawet gdy jakiś sterownik jądra już go "zajął" - tu nie jest potrzebne.
I2C_SLAVE = 0x0703

# ------------------------------------------------------------ komendy PN532
CMD_GET_FIRMWARE_VERSION = 0x02
CMD_SAM_CONFIGURATION = 0x14
CMD_RF_CONFIGURATION = 0x32
CMD_IN_LIST_PASSIVE_TARGET = 0x4A
CMD_IN_DATA_EXCHANGE = 0x40

# komendy wysyłane do samej karty przez InDataExchange
CARD_CMD_READ = 0x30
CARD_CMD_AUTH_KEY_A = 0x60
CARD_CMD_AUTH_KEY_B = 0x61

ACK_FRAME = b"\x00\x00\xff\x00\xff\x00"

# najpopularniejsze klucze fabryczne/domyślne MIFARE Classic
DEFAULT_KEYS = (
    bytes.fromhex("FFFFFFFFFFFF"),  # klucz fabryczny (większość pustych kart)
    bytes.fromhex("A0A1A2A3A4A5"),  # typowy klucz MAD / NDEF (NFC Forum)
    bytes.fromhex("D3F7D3F7D3F7"),  # typowy klucz B dla sektorów NDEF
    bytes.fromhex("000000000000"),  # czasem spotykany klucz "wyzerowany"
)

Tag = namedtuple("Tag", "uid atqa sak tg")

SAK_NAMES = {
    0x00: "MIFARE Ultralight / NTAG",
    0x08: "MIFARE Classic 1K",
    0x09: "MIFARE Mini",
    0x10: "MIFARE Plus 2K",
    0x11: "MIFARE Plus 4K",
    0x18: "MIFARE Classic 4K",
    0x20: "ISO14443-4 (np. DESFire, karta zbliżeniowa)",
    0x28: "ISO14443-4 + MIFARE Classic 1K (np. SmartMX/JCOP)",
    0x88: "MIFARE Classic 1K (wariant SAK)",
}


class PN532Error(Exception):
    """Błąd komunikacji z PN532."""


class PN532I2C:
    """Minimalny sterownik PN532 po I2C (ramki normalne, tryb ISO14443A)."""

    def __init__(self, bus, address):
        import fcntl  # import lokalnie: moduł pozostaje importowalny także na Windows

        self._fd = os.open("/dev/i2c-%d" % bus, os.O_RDWR)
        try:
            # patrz komentarz przy stałej I2C_SLAVE powyżej
            fcntl.ioctl(self._fd, I2C_SLAVE, address)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    # ------------------------------------------------------------ niski poziom
    def _write(self, data):
        last_error = None
        for _ in range(10):  # PN532 potrafi NACK-ować, gdy jest zajęty
            try:
                os.write(self._fd, data)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.02)
        raise PN532Error("Zapis I2C nie powiódł się: %s" % last_error)

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
        """Czyta bajt statusu + size bajtów; zwraca same dane (bez statusu)."""
        try:
            raw = os.read(self._fd, size + 1)
        except OSError as exc:
            raise PN532Error("Odczyt I2C nie powiódł się: %s" % exc)
        if not raw or not (raw[0] & 0x01):
            raise PN532Error("Moduł nie jest gotowy do odczytu")
        return raw[1:]

    # ------------------------------------------------------------ ramki
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
            raise PN532Error("Nieprawidłowa ramka odpowiedzi: %s" % raw.hex())
        length = raw[start + 3]
        if (length + raw[start + 4]) & 0xFF:
            raise PN532Error("Błędna suma kontrolna długości ramki")
        if length == 1:
            raise PN532Error("PN532 zgłosił błąd aplikacyjny (ramka błędu)")
        end = start + 5 + length
        if len(raw) < end + 1:
            raise PN532Error("Ramka odpowiedzi ucięta")
        data = raw[start + 5:end]
        if (sum(data) + raw[end]) & 0xFF:
            raise PN532Error("Błędna suma kontrolna danych")
        if data[0] != 0xD5 or data[1] != command + 1:
            raise PN532Error("Nieoczekiwana odpowiedź: %s" % data.hex())
        return bytes(data[2:])

    def call(self, command, params=b"", timeout=1.0, response_size=32):
        """Wysyła komendę, sprawdza ACK i zwraca dane odpowiedzi (bez D5 xx)."""
        self._write(self._build_frame(command, params))
        if not self._wait_ready(0.5):
            raise PN532Error("Brak ACK od PN532 (sprawdź okablowanie i tryb I2C)")
        ack = self._read_frame(len(ACK_FRAME))
        if ack != ACK_FRAME:
            raise PN532Error("Nieprawidłowe ACK: %s" % ack.hex())
        if not self._wait_ready(timeout):
            raise PN532Error("Przekroczono czas oczekiwania na odpowiedź")
        return self._parse_response(self._read_frame(response_size), command)

    # ------------------------------------------------------------ wysoki poziom
    def firmware_version(self):
        data = self.call(CMD_GET_FIRMWARE_VERSION)
        if len(data) < 4:
            raise PN532Error("Za krótka odpowiedź GetFirmwareVersion")
        return data[0], data[1], data[2], data[3]  # IC, Ver, Rev, Support

    def sam_configuration(self):
        # tryb normalny, timeout 1 s (0x14 * 50 ms), linia IRQ włączona
        self.call(CMD_SAM_CONFIGURATION, b"\x01\x14\x01")

    def set_passive_activation_retries(self, retries):
        # domyślnie 0xFF = czekaj w nieskończoność; skończona liczba prób
        # sprawia, że InListPassiveTarget wraca, gdy nie ma karty
        self.call(CMD_RF_CONFIGURATION, bytes([0x05, 0xFF, 0x01, retries]))

    def read_passive_target(self, timeout=1.0):
        """Zwraca Tag (ISO14443A 106 kbps) albo None, gdy brak karty w polu."""
        data = self.call(
            CMD_IN_LIST_PASSIVE_TARGET, b"\x01\x00", timeout=timeout, response_size=40
        )
        if not data or data[0] == 0:
            return None
        if len(data) < 6:
            raise PN532Error("Za krótka odpowiedź InListPassiveTarget")
        tg = data[1]
        uid_len = data[5]
        uid = bytes(data[6:6 + uid_len])
        if len(uid) != uid_len:
            raise PN532Error("Ucięty identyfikator UID")
        return Tag(uid=uid, atqa=bytes(data[2:4]), sak=data[4], tg=tg)

    # -------------------------------------------------- InDataExchange (karta)
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
    """Zwraca listę (nr_sektora, liczba_bloków_w_sektorze) albo None, gdy
    SAK nie odpowiada znanemu układowi MIFARE Classic."""
    if sak == 0x09:                 # Mini, 0.3K
        return [(s, 4) for s in range(5)]
    if sak in (0x08, 0x88):         # Classic 1K
        return [(s, 4) for s in range(16)]
    if sak == 0x18:                 # Classic 4K
        return [(s, 4) for s in range(32)] + [(s, 16) for s in range(32, 40)]
    return None


def try_authenticate_sector(reader, tg, trailer_block, uid):
    """Próbuje domyślnych kluczy (najpierw A, potem B) na danym sektorze."""
    for key_type in (CARD_CMD_AUTH_KEY_A, CARD_CMD_AUTH_KEY_B):
        for key in DEFAULT_KEYS:
            if reader.mifare_authenticate(tg, trailer_block, uid, key, key_type):
                return key_type, key
    return None, None


def dump_mifare_classic(reader, tag):
    """Odczytuje wszystkie dostępne sektory kluczami domyślnymi i wypisuje
    zawartość blok po bloku. Zwraca słownik {nr_bloku: 16 bajtów}."""
    layout = classic_sector_layout(tag.sak)
    if layout is None:
        print("  Nieznany układ sektorów dla SAK 0x%02X - pomijam pełny odczyt." % tag.sak)
        return {}

    print("  Pełny zrzut MIFARE Classic (%d sektorów, domyślne klucze)..." % len(layout))
    dump = {}
    abs_block = 0
    for sector_index, blocks_in_sector in layout:
        trailer_block = abs_block + blocks_in_sector - 1
        key_type, key = try_authenticate_sector(reader, tag.tg, trailer_block, tag.uid)
        if key is None:
            print("  Sektor %2d: brak dostępu (żaden z domyślnych kluczy nie pasuje)" % sector_index)
            abs_block += blocks_in_sector
            continue

        key_label = "A" if key_type == CARD_CMD_AUTH_KEY_A else "B"
        print("  Sektor %2d (klucz %s = %s):" % (sector_index, key_label, key.hex().upper()))
        for b in range(blocks_in_sector):
            block_num = abs_block + b
            data = reader.mifare_read_block(tag.tg, block_num)
            tag_label = " [trailer]" if b == blocks_in_sector - 1 else ""
            if data is None:
                print("    Blok %3d%s: <błąd odczytu>" % (block_num, tag_label))
                continue
            dump[block_num] = data
            print("    Blok %3d%s: %s" % (block_num, tag_label, data.hex().upper()))
        abs_block += blocks_in_sector
    return dump


# ----------------------------------------------- MIFARE Ultralight / NTAG

def dump_ultralight(reader, tag, max_pages=ULTRALIGHT_MAX_PAGES):
    """Czyta po 4 strony (16 B) na komendę READ, aż napotka błąd / limit.
    Zwraca słownik {nr_strony: 4 bajty}."""
    print("  Odczyt pamięci Ultralight/NTAG (strony po 4 B)...")
    pages = {}
    page = 0
    while page < max_pages:
        data = reader.call(
            CMD_IN_DATA_EXCHANGE,
            bytes([tag.tg, CARD_CMD_READ, page]),
            response_size=20,
        )
        if not data or data[0] != 0x00 or len(data) < 17:
            # pierwszy błąd zwykle oznacza koniec pamięci użytkownika - koniec odczytu
            break
        chunk = data[1:17]
        for i in range(4):
            p = page + i
            if p >= max_pages:
                break
            page_bytes = chunk[i * 4:(i + 1) * 4]
            pages[p] = page_bytes
            print("    Strona %3d: %s" % (p, page_bytes.hex().upper()))
        page += 4

    text = extract_ndef_text(pages)
    if text is not None:
        print("  --> Odczytany tekst NDEF: %r" % text)
    return pages


def extract_ndef_text(pages):
    """Najprostsza próba znalezienia rekordu NDEF typu Text w danych
    użytkownika (strony od 4. wzwyż). Zwraca tekst albo None."""
    if not pages:
        return None
    max_page = max(pages)
    buf = bytearray()
    for p in range(4, max_page + 1):
        buf += pages.get(p, b"\x00\x00\x00\x00")

    i = 0
    while i + 1 < len(buf):
        tlv_type = buf[i]
        if tlv_type == 0x00:      # NULL TLV - wypełnienie
            i += 1
            continue
        if tlv_type == 0xFE:      # terminator pamięci NDEF
            break
        length = buf[i + 1]
        value_start = i + 2
        if length == 0xFF:        # format długi (rzadki na małych tagach)
            if i + 3 >= len(buf):
                break
            length = (buf[i + 2] << 8) | buf[i + 3]
            value_start = i + 4
        if tlv_type == 0x03:      # wiadomość NDEF
            text = parse_ndef_text_record(bytes(buf[value_start:value_start + length]))
            if text is not None:
                return text
        i = value_start + length
    return None


def parse_ndef_text_record(ndef):
    """Rozpakowuje pierwszy krótki rekord NDEF typu 'T' (Text)."""
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
    """Dobiera właściwą procedurę odczytu na podstawie SAK."""
    try:
        if tag.sak == 0x00:
            dump_ultralight(reader, tag)
        elif tag.sak in (0x08, 0x09, 0x18, 0x88):
            dump_mifare_classic(reader, tag)
        else:
            print("  Pełny odczyt danych nieobsługiwany dla tego typu karty (SAK 0x%02X)." % tag.sak)
    except PN532Error as exc:
        print("  Przerwano odczyt danych: %s" % exc)


def run(ctx):
    print("=== Czytnik RFID PN532 (I2C, bus %d, adres 0x%02X) ===" % (I2C_BUS, PN532_ADDRESS))

    try:
        reader = PN532I2C(I2C_BUS, PN532_ADDRESS)
    except ImportError:
        print("Ten skrypt wymaga Linuksa (moduł fcntl / urządzenie /dev/i2c-N).")
        return 1
    except FileNotFoundError:
        print("Brak /dev/i2c-%d - włącz I2C: sudo raspi-config -> Interface Options -> I2C." % I2C_BUS)
        return 1
    except PermissionError:
        print("Brak uprawnień do /dev/i2c-%d - dodaj użytkownika do grupy i2c:" % I2C_BUS)
        print("  sudo usermod -aG i2c $USER   (i zaloguj się ponownie)")
        return 1
    except OSError as exc:
        print("Nie można otworzyć magistrali I2C: %s" % exc)
        return 1

    try:
        ic, ver, rev, _support = reader.firmware_version()
        print("Wykryto PN5%02X, firmware %d.%d" % (ic, ver, rev))
        reader.sam_configuration()
        reader.set_passive_activation_retries(0x02)

        print("Przyłóż kartę/brelok do czytnika. Ctrl+C kończy.\n")
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
                    raise PN532Error("Zbyt wiele błędów z rzędu, ostatni: %s" % exc)
                time.sleep(0.2)
                continue

            if tag is None:
                absent += 1
                if absent >= ABSENT_POLLS_TO_RESET and last_uid is not None:
                    print("Karta zdjęta.\n")
                    last_uid = None
            else:
                absent = 0
                if tag.uid != last_uid:
                    last_uid = tag.uid
                    kind = SAK_NAMES.get(tag.sak, "nieznany typ")
                    print("UID: %s  (%d B)" % (format_uid(tag.uid), len(tag.uid)))
                    print("  ATQA: %s  SAK: 0x%02X  -> %s" % (tag.atqa.hex().upper(), tag.sak, kind))
                    dump_tag_data(reader, tag)
                    print()
            time.sleep(POLL_PAUSE)

    except KeyboardInterrupt:
        print("\nZakończono odczyt.")
        return 0
    except PN532Error as exc:
        print("Błąd PN532: %s" % exc)
        return 2
    finally:
        reader.close()