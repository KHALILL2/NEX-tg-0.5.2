import time
import sys

print("----------------------------------------")
print("RC522 Hardware Diagnostic Tool")
print("----------------------------------------")

try:
    import RPi.GPIO as GPIO
    from mfrc522 import MFRC522
except ImportError:
    print("[FAIL] Could not import mfrc522 or RPi.GPIO.")
    sys.exit(1)

print("[1] Libraries loaded successfully.")

try:
    # Initialize without interacting with the card
    reader = MFRC522()
    print("[2] MFRC522 object created.")
    
    # Force a hardware reset
    pin = getattr(reader, 'pin_rst', 22)
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, 0)
    time.sleep(0.1)
    GPIO.output(pin, 1)
    time.sleep(0.1)
    print(f"[3] Hardware reset pulse sent to BOARD pin {pin}.")

    # Read the VersionReg (Register 0x37)
    # The MFRC522_Read method takes the register address. 
    # VersionReg is 0x37.
    version = reader.Read_MFRC522(0x37)
    
    print("\n--- DIAGNOSTIC RESULTS ---")
    print(f"Version Register returned: 0x{version:02X}")
    
    if version == 0x00 or version == 0xFF:
        print("\n[ERROR] The Raspberry Pi cannot talk to the RC522 chip at all.")
        print("This means the SPI wires (SDA, SCK, MOSI, MISO) are disconnected/broken,")
        print("or the RC522 chip itself is permanently dead.")
    elif version in (0x91, 0x92):
        print("\n[SUCCESS] The RC522 chip is ALIVE and talking to the Pi!")
        print("Version is MFRC522 v1.0 or v2.0.")
        print("If cards still don't read, the copper antenna trace on the board is broken.")
    else:
        print("\n[WARNING] Unknown version. Wiring might be loose causing corrupted data.")
        
except Exception as e:
    print(f"\n[EXCEPTION] {e}")
finally:
    GPIO.cleanup()
