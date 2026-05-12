#!/usr/bin/env python3
import time
import sys
try:
    from mfrc522 import MFRC522
except ImportError:
    print("mfrc522 library not found. Please install it with: pip3 install mfrc522 --break-system-packages")
    sys.exit(1)

def write_card():
    # 1. Get the student ID from the administrator
    print("========================================")
    print("   MIFARE CARD PROVISIONING UTILITY     ")
    print("========================================")
    student_id = input("Enter 7-digit Student Seat Number (e.g. 2420407): ").strip()
    
    if not student_id.isdigit() or len(student_id) < 1:
        print("Invalid ID. Please enter a valid number.")
        return

    # 2. Prepare the 16-byte block data
    # We pad the string with null bytes (0x00) up to 16 bytes
    byte_array = bytearray(student_id.encode('ascii'))
    if len(byte_array) > 16:
        print("ID is too long (max 16 characters).")
        return
    
    while len(byte_array) < 16:
        byte_array.append(0x00)

    # Convert to list of ints for MFRC522 library
    data_to_write = list(byte_array)

    # 3. Initialize reader (using CE1)
    reader = MFRC522(bus=0, device=1)
    print("\n[INFO] Reader initialized on CE1.")
    print(f"[INFO] Ready to program ID: {student_id}")
    print(">>> Please TAP AND HOLD a blank MIFARE card to the reader...")

    # We will write to Sector 1, Block 4
    # (Sector 0 Block 0 is read-only UID, Block 3, 7, 11 are Sector Trailers containing passwords)
    target_block = 4
    default_key = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]

    try:
        while True:
            # Look for card
            status, tag_type = reader.MFRC522_Request(reader.PICC_REQIDL)
            if status != reader.MI_OK:
                time.sleep(0.1)
                continue

            # Anti-collision to get UID
            status, uid = reader.MFRC522_Anticoll()
            if status != reader.MI_OK:
                continue

            print(f"\n[+] Card Detected! UID: {uid[:4]}")
            
            # Select the card
            reader.MFRC522_SelectTag(uid)

            # Authenticate to Sector 1 (Block 4) using default Key A
            status = reader.MFRC522_Auth(reader.PICC_AUTHENT1A, target_block, default_key, uid)
            if status == reader.MI_OK:
                # Write the data
                print(f"[*] Writing '{student_id}' to Block {target_block}...")
                reader.MFRC522_Write(target_block, data_to_write)
                
                # Read it back to verify
                print("[*] Verifying write...")
                read_data = reader.MFRC522_Read(target_block)
                if read_data:
                    # Strip trailing null bytes to check the string
                    verified_string = bytes(read_data).partition(b'\x00')[0].decode('ascii')
                    if verified_string == student_id:
                        print(f"[SUCCESS] Card successfully programmed with ID: {verified_string}")
                        print("\nYou can now remove the card.")
                        reader.MFRC522_StopCrypto1()
                        break
                    else:
                        print(f"[ERROR] Verification failed. Read: {verified_string}")
                else:
                    print("[ERROR] Failed to read back the written block.")
                
                reader.MFRC522_StopCrypto1()
                break
            else:
                print("[ERROR] Authentication failed! This card might not be a standard MIFARE 1K or the password was changed.")
                break

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        pass

if __name__ == "__main__":
    write_card()
