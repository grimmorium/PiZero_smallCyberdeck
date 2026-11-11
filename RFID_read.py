import xml.etree.ElementTree as ET
import time
import spidev
from gpiozero import OutputDevice

# --- BARE-METAL DRIVER FOR THE MFRC522 ---
# SPI communication implementation without any external mfrc522 library

class MinimalRC522:
    def __init__(self, bus, device, speed, rst_pin):
        # Initialize SPI
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed

        # Initialize the reset pin (on Bookworm gpiozero defaults to the safer gpiod/lgpio backend)
        self.rst = OutputDevice(rst_pin)

        self._hw_reset()
        self._init_registers()

    def _hw_reset(self):
        self.rst.off()
        time.sleep(0.05)
        self.rst.on()
        time.sleep(0.05)
        # SoftReset via CommandReg (0x01)
        self._write_reg(0x01, 0x0F)
        time.sleep(0.05)

    def _write_reg(self, reg, val):
        # Format the address for writing (shift left, clear the least and most significant bits)
        self.spi.xfer2([ (reg << 1) & 0x7E, val ])

    def _read_reg(self, reg):
        # Format the address for reading (most significant bit = 1)
        return self.spi.xfer2([ ((reg << 1) & 0x7E) | 0x80, 0 ])[1]

    def _init_registers(self):
        self._write_reg(0x2A, 0x8D) # TModeReg - timer settings
        self._write_reg(0x2B, 0x3E) # TPrescalerReg
        self._write_reg(0x2D, 30)   # TReloadRegL (low byte)
        self._write_reg(0x2C, 0)    # TReloadRegH (high byte)
        self._write_reg(0x15, 0x40) # TxAutoReg - 100% ASK
        self._write_reg(0x11, 0x3D) # ModeReg

        # Enable the antenna
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
            
        # Wait for interrupt/completion
        i = 2000
        while i > 0:
            n = self._read_reg(0x04) # ComIrqReg
            if (n & 0x01) or (n & wait_irq):
                break
            i -= 1

        self._write_reg(0x0D, self._read_reg(0x0D) & (~0x80)) # StopSend

        status, back_data = False, []
        if i != 0 and (self._read_reg(0x06) & 0x1B) == 0x00: # Check ErrorReg
            status = True
            n = self._read_reg(0x0A) # How many bytes are in the FIFO
            if n == 0: n = 1
            if n > 16: n = 16
            for _ in range(n):
                back_data.append(self._read_reg(0x09))
        return status, back_data

    def scan_for_card(self):
        """Step 1: REQIDL - look for a card within field range"""
        self._write_reg(0x0D, 0x07) # BitFramingReg
        status, data = self._communicate(0x0C, [0x26])
        return status and len(data) == 2

    def read_uid(self):
        """Step 2: ANTICOLL - resolve collisions and read the serial number"""
        self._write_reg(0x0D, 0x00) # BitFramingReg
        status, data = self._communicate(0x0C, [0x93, 0x20])
        if status and len(data) == 5:
            # Verify the checksum (BCC)
            bcc = 0
            for i in range(4): bcc ^= data[i]
            if bcc == data[4]:
                return data[:4] # Return the 4-byte UID
        return None

    def close(self):
        self.spi.close()
        self.rst.close()


# --- FUNCTIONS FOR PYAPPSRUNNER ---

def get_config(xml_path):
    """
    Reads the XML file and returns a tuple (bus, device, speed, rst_pin).
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

    raise ValueError("No 'RC522' section in the XML.")

def run(ctx) -> int:
    """
    Entry point. The PyAppsRunner menu expects an int return value; 0 means success.
    """
    xml_path = ctx.base_dir / "interfaces_config.xml"
    if not xml_path.exists():
        print(f"Error: file not found: {xml_path}")
        return 1

    try:
        bus, device, speed, rst_pin = get_config(xml_path)
    except Exception as e:
        print(f"Error parsing the configuration from XML: {e}")
        return 1

    print(f"[*] Hardware parameters: SPI{bus}.{device} @ {speed//1000}kHz | RST=GPIO{rst_pin}")
    print("[*] Starting up the MFRC522 driver (bare-metal)...")

    try:
        # Hardware initialization with parameters read from the file
        rc522 = MinimalRC522(bus, device, speed, rst_pin)
    except Exception as e:
        print(f"Hardware access error (SPI/GPIO): {e}")
        print("Make sure the SPI interface (e.g., spi1-3cs) is enabled in the system.")
        return 1

    print("\nReady. Hold an RFID card to the reader.")
    print("Press Ctrl+C to stop and return to the menu.")
    
    last_uid = None
    try:
        while True:
            # First send a search request (REQIDL)
            if rc522.scan_for_card():
                # Card detected, proceed to reading the UID
                uid = rc522.read_uid()

                if uid:
                    uid_str = "-".join([f"{x:02X}" for x in uid])
                    if uid_str != last_uid:
                        print(f"[+] New card! UID (hex): {uid_str}")
                        last_uid = uid_str
            else:
                # If the card leaves the field, allow re-reading the same one
                last_uid = None

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nReading stopped at user request (returning to the menu).")
    except Exception as e:
        print(f"\nAn error occurred in the read loop: {e}")
        return 1
    finally:
        rc522.close()

    return 0