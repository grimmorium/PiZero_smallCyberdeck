import xml.etree.ElementTree as ET
import time
import spidev
from gpiozero import OutputDevice

# --- BARE-METAL STEROWNIK MFRC522 ---
# Implementacja komunikacji po SPI, bez zewnętrznych bibliotek mfrc522

class MinimalRC522:
    def __init__(self, bus, device, speed, rst_pin):
        # Inicjalizacja SPI
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed
        
        # Inicjalizacja pinu resetu (domyślnie na Bookworm używa bezpiecznego gpiod/lgpio)
        self.rst = OutputDevice(rst_pin)
        
        self._hw_reset()
        self._init_registers()

    def _hw_reset(self):
        self.rst.off()
        time.sleep(0.05)
        self.rst.on()
        time.sleep(0.05)
        # SoftReset poprzez CommandReg (0x01)
        self._write_reg(0x01, 0x0F)
        time.sleep(0.05)

    def _write_reg(self, reg, val):
        # Formatowanie adresu do zapisu (przesunięcie w lewo i zerowanie najmłodszego oraz najstarszego bitu)
        self.spi.xfer2([ (reg << 1) & 0x7E, val ])

    def _read_reg(self, reg):
        # Formatowanie adresu do odczytu (najstarszy bit = 1)
        return self.spi.xfer2([ ((reg << 1) & 0x7E) | 0x80, 0 ])[1]

    def _init_registers(self):
        self._write_reg(0x2A, 0x8D) # TModeReg - timer settings
        self._write_reg(0x2B, 0x3E) # TPrescalerReg
        self._write_reg(0x2D, 30)   # TReloadRegL (niski bajt)
        self._write_reg(0x2C, 0)    # TReloadRegH (wysoki bajt)
        self._write_reg(0x15, 0x40) # TxAutoReg - 100% ASK
        self._write_reg(0x11, 0x3D) # ModeReg
        
        # Włączenie anteny
        tmp = self._read_reg(0x14) # TxControlReg
        if not (tmp & 0x03):
            self._write_reg(0x14, tmp | 0x03)

    def _communicate(self, command, send_data):
        irq_en, wait_irq = 0x00, 0x00
        if command == 0x0C: # Transceive
            irq_en, wait_irq = 0x77, 0x30
            
        self._write_reg(0x02, irq_en | 0x80) # ComIEnReg
        self._write_reg(0x04, self._read_reg(0x04) & (~0x80)) # Clear ComIrqReg bits
        self._write_reg(0x0A, self._read_reg(0x0A) | 0x80)    # Flush FIFOLevelReg
        self._write_reg(0x01, 0x00) # CommandReg -> Idle
        
        for byte in send_data:
            self._write_reg(0x09, byte) # FIFODataReg
            
        self._write_reg(0x01, command) # Execute command
        if command == 0x0C:
            self._write_reg(0x0D, self._read_reg(0x0D) | 0x80) # StartSend (BitFramingReg)
            
        # Oczekiwanie na przerwanie/zakończenie
        i = 2000
        while i > 0:
            n = self._read_reg(0x04) # ComIrqReg
            if (n & 0x01) or (n & wait_irq):
                break
            i -= 1
            
        self._write_reg(0x0D, self._read_reg(0x0D) & (~0x80)) # StopSend
        
        status, back_data = False, []
        if i != 0 and (self._read_reg(0x06) & 0x1B) == 0x00: # Sprawdzanie ErrorReg
            status = True
            n = self._read_reg(0x0A) # Ile bajtów w FIFO
            if n == 0: n = 1
            if n > 16: n = 16
            for _ in range(n):
                back_data.append(self._read_reg(0x09))
        return status, back_data

    def scan_for_card(self):
        """Krok 1: REQIDL - Szukanie karty w zasięgu pola"""
        self._write_reg(0x0D, 0x07) # BitFramingReg
        status, data = self._communicate(0x0C, [0x26])
        return status and len(data) == 2

    def read_uid(self):
        """Krok 2: ANTICOLL - Rozwiązanie kolizji i odczyt numeru seryjnego"""
        self._write_reg(0x0D, 0x00) # BitFramingReg
        status, data = self._communicate(0x0C, [0x93, 0x20])
        if status and len(data) == 5:
            # Weryfikacja sumy kontrolnej (BCC)
            bcc = 0
            for i in range(4): bcc ^= data[i]
            if bcc == data[4]:
                return data[:4] # Zwraca 4 bajty UID
        return None

    def close(self):
        self.spi.close()
        self.rst.close()


# --- FUNKCJE DLA PYAPPSRUNNER ---

def get_config(xml_path):
    """
    Czyta plik XML i zwraca tuple (bus, device, speed, rst_pin).
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    for spi_dev in root.findall('spi_device'):
        if spi_dev.get('name') == 'RC522':
            bus = int(spi_dev.get('bus', 1))
            device = int(spi_dev.get('device', 0))
            speed = int(spi_dev.get('speed_hz', 1000000))
            
            rst_pin = 22
            for pin in spi_dev.findall('pin'):
                if pin.get('name') == 'RST':
                    rst_pin = int(pin.get('gpio'))
                    
            return bus, device, speed, rst_pin
            
    raise ValueError("Brak sekcji 'RC522' w XML.")

def run(ctx) -> int:
    """
    Punkt wejścia. Menu PyAppsRunner oczekuje powrotu liczby (int), 0 to sukces[cite: 2].
    """
    xml_path = ctx.base_dir / "interfaces_config.xml"
    if not xml_path.exists():
        print(f"Błąd: Nie znaleziono pliku {xml_path}")
        return 1

    try:
        bus, device, speed, rst_pin = get_config(xml_path)
    except Exception as e:
        print(f"Błąd parsowania konfiguracji z XML: {e}")
        return 1

    print(f"[*] Parametry sprzętowe: SPI{bus}.{device} @ {speed//1000}kHz | RST=GPIO{rst_pin}")
    print("[*] Budzenie sterownika MFRC522 (Bare-Metal)...")
    
    try:
        # Inicjalizacja sprzętowa z parametrami wczytanymi z pliku[cite: 1]
        rc522 = MinimalRC522(bus, device, speed, rst_pin)
    except Exception as e:
        print(f"Błąd dostępu do sprzętu (SPI/GPIO): {e}")
        print("Upewnij się, że interfejs SPI (np. spi1-3cs) jest włączony w systemie.")
        return 1

    print("\nGotowe. Zbliż kartę RFID do czytnika.")
    print("Naciśnij Ctrl+C aby przerwać i wrócić do menu.[cite: 2]")
    
    last_uid = None
    try:
        while True:
            # Wpierw rzucamy żądanie wyszukania (REQIDL)
            if rc522.scan_for_card():
                # Karta wykryta, przechodzimy do odczytu UID
                uid = rc522.read_uid()
                
                if uid:
                    uid_str = "-".join([f"{x:02X}" for x in uid])
                    if uid_str != last_uid:
                        print(f"[+] Nowa karta! UID (Hex): {uid_str}")
                        last_uid = uid_str
            else:
                # Jeśli karta zniknie z pola, pozwalamy na ponowne zaczytanie tej samej
                last_uid = None
                
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nZatrzymano odczyt na żądanie użytkownika (powrót do menu).[cite: 2]")
    except Exception as e:
        print(f"\nWystąpił błąd w pętli odczytu: {e}")
        return 1
    finally:
        rc522.close()

    return 0