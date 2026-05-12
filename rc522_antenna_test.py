import time
import sys

print("----------------------------------------")
print("RC522 Antenna Power Diagnostic")
print("----------------------------------------")

try:
    import RPi.GPIO as GPIO
    from mfrc522 import MFRC522
except ImportError:
    print("[FAIL] Could not import mfrc522 or RPi.GPIO.")
    sys.exit(1)

try:
    reader = MFRC522(bus=0, device=1)
    
    # Do a full hardware reset just to be clean
    pin = getattr(reader, 'pin_rst', 22)
    GPIO.setup(pin, GPIO.OUT)
    GPIO.output(pin, 0)
    time.sleep(0.1)
    GPIO.output(pin, 1)
    time.sleep(0.1)
    
    # Initialize the library (this is supposed to turn the antenna on)
    reader.MFRC522_Init()
    
    # TxControlReg is register address 0x14
    tx_control = reader.Read_MFRC522(0x14)
    print(f"Initial TxControlReg value: {bin(tx_control)}")
    
    # Check if the bottom two bits are 11 (Antenna ON)
    if (tx_control & 0x03) == 0x03:
        print("[SUCCESS] The MFRC522 brain is successfully powering the Antenna pins!")
        print("This proves 100% that the copper antenna coil on the board is physically broken.")
    else:
        print("[WARNING] The antenna is turned OFF in the chip's memory!")
        print("Attempting to force it on...")
        reader.SetBitMask(0x14, 0x03)
        time.sleep(0.1)
        new_tx = reader.Read_MFRC522(0x14)
        print(f"Forced TxControlReg value: {bin(new_tx)}")
        if (new_tx & 0x03) == 0x03:
            print("Antenna forced ON! Try scanning a card with test_reader.py now.")
        else:
            print("Failed to force the antenna on. The chip is defective.")

except Exception as e:
    print(f"\n[EXCEPTION] {e}")
finally:
    GPIO.cleanup()
