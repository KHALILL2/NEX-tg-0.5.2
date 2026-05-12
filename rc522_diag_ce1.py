import time
import sys

print("----------------------------------------")
print("RC522 Hardware Diagnostic Tool (CE1 TEST)")
print("----------------------------------------")

try:
    import RPi.GPIO as GPIO
    from mfrc522 import MFRC522
except ImportError:
    print("[FAIL] Could not import mfrc522 or RPi.GPIO.")
    sys.exit(1)

try:
    # Initialize MFRC522 on SPI bus 0, device 1 (CE1)
    reader = MFRC522(bus=0, device=1)
    print("[1] MFRC522 object created on Device 1 (CE1).")
    
    # Force a hardware reset
    pin = getattr(reader, 'pin_rst', 22)
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, 0)
    time.sleep(0.1)
    GPIO.output(pin, 1)
    time.sleep(0.1)
    print(f"[2] Hardware reset pulse sent to BOARD pin {pin}.")

    version = reader.Read_MFRC522(0x37)
    
    print("\n--- DIAGNOSTIC RESULTS ---")
    print(f"Version Register returned: 0x{version:02X}")
    
    if version == 0x00 or version == 0xFF:
        print("\n[ERROR] Still getting 0x00 on CE1.")
    elif version in (0x91, 0x92):
        print("\n[SUCCESS] The RC522 chip is ALIVE! Your CE0 pin was burnt out!")
    else:
        print("\n[WARNING] Unknown version.")
        
except Exception as e:
    print(f"\n[EXCEPTION] {e}")
finally:
    GPIO.cleanup()
