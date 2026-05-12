import spidev
import sys

print("----------------------------------------")
print("Raspberry Pi SPI Loopback Test")
print("----------------------------------------")
print("WARNING: Before running this, you MUST:")
print("1. Unplug the RC522 module entirely.")
print("2. Use a single jumper wire to connect the Pi's MOSI pin directly to its MISO pin.")
print("   (Usually Pin 19 connected to Pin 21)")
print("----------------------------------------\n")

try:
    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 1000000
except Exception as e:
    print(f"[FAIL] Could not open SPI bus: {e}")
    print("Make sure SPI is enabled in raspi-config!")
    sys.exit(1)

# Send some test bytes
test_data = [0xC0, 0xFF, 0xEE, 0x12, 0x34]
print(f"Sending data:  {test_data}")

try:
    # xfer2 sends and receives simultaneously
    received_data = spi.xfer2(test_data)
    print(f"Received data: {received_data}")
    
    if received_data == test_data:
        print("\n[SUCCESS] The Raspberry Pi's SPI hardware is working PERFECTLY.")
        print("This confirms the issue is 100% either the RC522 chip being burnt")
        print("or the jumper wires to the RC522 being broken/loose.")
    elif all(b == 0 for b in received_data):
        print("\n[ERROR] Received all zeros (0x00).")
        print("If you connected MOSI to MISO, the Pi's SPI might be broken,")
        print("or the jumper wire you used for the loopback is bad.")
    else:
        print("\n[WARNING] Received corrupted data. SPI hardware might be failing.")

except Exception as e:
    print(f"\n[EXCEPTION] {e}")
finally:
    spi.close()
