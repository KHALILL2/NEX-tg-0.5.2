import time
import sys

try:
    from mfrc522 import MFRC522
except ImportError:
    print("Error: mfrc522 library not found!")
    print("Please run: pip install mfrc522 spidev RPi.GPIO")
    sys.exit(1)

def main():
    print("======================================")
    print("   RC522 RAW HARDWARE TEST SCRIPT")
    print("======================================")
    print("Initializing reader...")
    
    # Initialize exactly as gate.py does
    reader = MFRC522()
    
    print("Waiting for you to scan a card... (Press Ctrl+C to exit)\n")
    
    try:
        while True:
            # 1. Look for a card
            (status, TagType) = reader.MFRC522_Request(reader.PICC_REQIDL)
            
            # 2. If card found, read its UID
            if status == reader.MI_OK:
                (status, uid_bytes) = reader.MFRC522_Anticoll()
                
                if status == reader.MI_OK:
                    # We got the bytes! Calculate the formats
                    
                    # Decimal (Big Endian - what we send to API)
                    decimal_uid = str(int.from_bytes(bytes(uid_bytes[:4]), byteorder="big"))
                    
                    # Hex Format (For your eyes)
                    hex_uid = ":".join(f"{b:02X}" for b in uid_bytes[:4])
                    
                    print(f"--- CARD DETECTED ---")
                    print(f"Raw Bytes : {uid_bytes[:4]}")
                    print(f"Hex ID    : {hex_uid}")
                    print(f"Decimal ID: {decimal_uid}  <-- This is what goes to the API")
                    print("---------------------\n")
                    
                    # Halt the card so it doesn't spam repeatedly while resting on the reader
                    reader.MFRC522_StopCrypto1()
                    time.sleep(1.5)  # Wait before allowing next scan
                    
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("\nExiting test script.")
    finally:
        try:
            import RPi.GPIO as GPIO
            GPIO.cleanup()
        except:
            pass

if __name__ == "__main__":
    main()
