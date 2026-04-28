<p align="center">
  <h1 align="center">🏛️ Gate Access Monitoring System</h1>
  <p align="center">
    <strong>BATU University — Smart Turnstile Gate Controller</strong><br>
    A fullscreen kiosk application running on Raspberry Pi 4B that verifies student IDs<br>
    via RFID scan, controls a physical turnstile gate, and displays student information on a monitor.
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Raspberry%20Pi%204B-c51a4a?logo=raspberrypi&logoColor=white" alt="RPi 4B">
  <img src="https://img.shields.io/badge/UI-CustomTkinter-1f6feb" alt="CustomTkinter">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

---

## Table of Contents

- [Overview](#overview)
- [Hardware Setup](#hardware-setup)
- [Software Architecture](#software-architecture)
- [Display Layout](#display-layout)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [Project Structure](#project-structure)
- [Arduino Protocol](#arduino-protocol)
- [API Contract](#api-contract)
- [Health Check Endpoint](#health-check-endpoint)
- [Security Features](#security-features)
- [Offline Fallback](#offline-fallback)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Overview

When a student approaches the university turnstile gate:

1. **RC522 RFID Module** reads the student's ID card.
2. **Raspberry Pi 4B** validates the input, then sends the UID to a remote REST API for verification (with automatic retry on transient failures).
3. If **access is granted**:
   - The monitor displays the student's photo, name, seat number, college, and department with a green "Access Granted" badge.
   - The Arduino unlocks the solenoid lock.
   - After a configurable delay, the Arduino re-locks the solenoid.
   - The result is cached locally for offline fallback (if enabled).
4. If **access is denied**:
   - The monitor shows a red "Access Denied" badge (never exposing the raw card UID).
   - No gate action occurs.
5. Previous scans are shown in a history row at the bottom of the screen.
6. If the API is unreachable and offline mode is enabled, recently-verified students can still pass through.

---

## Hardware Setup

| Component | Role | Connection |
|---|---|---|
| **Raspberry Pi 4B** | Main controller + display driver | HDMI to monitor |
| **RC522 RFID Module** | Primary input method for student ID cards | SPI GPIO pins on RPi |
| **ATmega / Arduino** | Medium between RPi and solenoid lock ONLY | USB Serial (`/dev/ttyACM0`) |
| **Solenoid Lock** | Electromagnetic gate lock | Controlled via relay by Arduino |
| **USB Barcode Scanner** | Alternative input method | USB HID / Serial |
| **HDMI Monitor** | Displays the kiosk GUI | HDMI |

> **Note:** The system uses a minimal hardware design:
> - The RC522 RFID reader connects directly to the Raspberry Pi via SPI
> - The Arduino serves only as a relay controller for the solenoid lock
> - No visual indicators (LEDs), audio feedback (buzzers), or position sensing (IR) is used
> - Gate state is tracked in software, not detected via sensors

### RC522 RFID Connection

The RC522 module connects **directly** to the Raspberry Pi GPIO header (SPI bus 0):

| RC522 Pin | RPi GPIO | Function |
|-----------|----------|----------|
| SDA (SS) | GPIO 8 (CE0) | Chip Select |
| SCK | GPIO 11 (SCLK) | SPI Clock |
| MOSI | GPIO 10 (MOSI) | Master Out Slave In |
| MISO | GPIO 9 (MISO) | Master In Slave Out |
| RST | GPIO 25 | Reset |
| GND | GND | Ground |
| 3.3V | 3.3V | Power (DO NOT use 5V!) |

*(Note: Arduino internal wiring for the solenoid relay is not documented here as it is internal to the Arduino firmware).*

---

## Software Architecture

```
┌──────────────┐     USB Serial      ┌──────────────────┐
│  ATmega /    │◄────────────────────►│  Raspberry Pi 4B │
│  Arduino     │  GATE:OPEN/CLOSE    │                  │
│              │                     │  gate.py         │
│              │                     │  (CustomTkinter) │
└──────┬───────┘                     │                  │
       │                             │   ┌──────────┐   │
       ▼           SPI GPIO          │   │  Monitor │   │
 Solenoid Lock ◄─────────────────────│   │  (HDMI)  │   │
                  RC522 RFID Module  │   └──────────┘   │
                                     │                  │
                                     │  ┌────────────┐  │
                                     │  │ REST API   │  │
                                     │  │ (HTTPS)    │  │
                                     │  └────────────┘  │
                                     └──────────────────┘
```

### Key Design Decisions

| Concern | Solution |
|---|---|
| **High Throughput** | `HighThroughputProcessor` thread isolates hardware polling from the UI, supporting up to 6,000 students/day (8-13 scans/min peak) with `< 1.5s` latency. |
| **Thread safety** | All Tkinter widget updates via `self.after(0, …)`. Debounce and state dictionaries are protected by `threading.Lock`. |
| **Input validation** | `UIDValidator` sanitizes inputs and uses HMAC-SHA256 hashing to ensure raw UIDs are never stored or logged in plain text. |
| **API resilience** | HTTP connection pooling (`urllib3`) speeds up rapid requests. Exponential-backoff retry handles transient errors. |
| **SSL security** | Configurable certificate pinning via `GATE_API_CERT_PATH`. SSL verification enabled by default. |
| **Arduino reconnection** | Background thread with exponential backoff (2 s → 60 s cap) auto-reconnects on serial loss. |
| **Duplicate scans** | Configurable debounce window (`CARD_DEBOUNCE_SECONDS`) rejects the same card within N seconds. |
| **Gate auto-close** | Timer automatically triggers `GATE:CLOSE` after `GATE_OPEN_DURATION` seconds. |
| **Offline fallback** | Optional time-limited cache of successful checks (keyed by hashed UID) ensures passage during API outages. |
| **Denied display** | Generic Arabic message shown; raw UID never appears on screen. |
| **Simulation Mode** | Software simulation of RFID reads for UI/API development without physical hardware. |
| **Configuration** | All values via environment variables. `GATE_API_URL` and `UID_HASH_KEY` are *required*. |

---

## Display Layout

The monitor shows a fullscreen dark-themed interface in Arabic (RTL):

```
┌──────────────────────────────────────────────────────┐
│  🏛️    نظام مراقبة بوابة الدخول   ● Arduino  12:30 PM │  Header
│         Gate Access Monitoring System                │
├──────────────────────────────────────────────────────┤
│                                                      │
│  ┌─ Status Badge ──────────────┐  ┌──────────────┐  │
│  │  ✓ الدخول مسموح             │  │              │  │
│  └─────────────────────────────┘  │   Student    │  │
│                                   │    Photo     │  │  Main Card
│  Student Name (Arabic, large)     │              │  │
│  Seat Number: 12345               │              │  │
│  كلية الهندسة                     └──────────────┘  │
│  تكنولوجيا المعلومات                                 │
│                          ⏰ 12:30:45 PM              │
│                                                      │
├──────────────────────────────────────────────────────┤
│                                     :آخر الحضور      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │  Photo   │  │  Photo   │  │  Photo   │           │  History
│  │  ━━━━━━  │  │  ━━━━━━  │  │  ━━━━━━  │           │  (3 cards)
│  │  Name    │  │  Name    │  │  Name    │           │
│  │  Seat    │  │  Seat    │  │  Seat    │           │
│  │  Time    │  │  Time    │  │  Time    │           │
│  └──────────┘  └──────────┘  └──────────┘           │
├──────────────────────────────────────────────────────┤
│  ✓ النظام جاهز - في انتظار مسح البطاقة...           │  Status Bar
└──────────────────────────────────────────────────────┘
```

---

## Installation

### Quick Install (Raspberry Pi OS)

```bash
git clone <your-repo-url> ~/gate-scanner
cd ~/gate-scanner
chmod +x install.sh
./install.sh
```

The installer will:
1. Install system packages (`python3-tk`, `fonts-amiri`, image libraries).
2. Create a Python virtual environment.
3. Install Python dependencies from `requirements.txt` and `requirements-rpi.txt`.
4. Create `config.env` from the example template.
5. Optionally install the systemd service for auto-start on boot.

### Manual Install

```bash
# System deps
sudo apt-get install python3 python3-venv python3-tk fonts-amiri

# Virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# If on Raspberry Pi:
pip install -r requirements-rpi.txt

# Config
cp config.env.example config.env
nano config.env
chmod 600 config.env # Secure your HMAC key
```

---

## Configuration

All settings are controlled via environment variables. Copy and edit the template:

```bash
cp config.env.example config.env
nano config.env
```

| Variable | Required | Description |
|---|:---:|---|
| `GATE_API_URL` | **Yes** | REST API endpoint for access checks |
| `UID_HASH_KEY` | **Yes** | 32-byte hex string for HMAC-SHA256 hashing of UIDs |
| `GATE_API_KEY` | No | Bearer token for API authentication |
| `GATE_API_UID_FIELD` | No | JSON field for API payload (default `card_uid`) |
| `GATE_VERIFY_SSL` | No | Enable/disable SSL certificate verification (default `true`) |
| `GATE_SERIAL_PORT` | No | Arduino Mega serial port (default `/dev/ttyACM0`) |
| `RFID_READER_TYPE` | No | `RC522`, `PN532`, or `SIMULATION` (default `RC522`) |
| `CARD_DEBOUNCE_SECONDS` | No | Duplicate scan ignore window (default `3`) |
| `GATE_OPEN_DURATION` | No | Seconds gate solenoid stays unlocked (default `5`) |
| `SIMULATION_MODE` | No | Software testing without hardware (default `false`) |
| `GATE_OFFLINE_MODE` | No | Enable offline cache fallback (default `false`) |

---

## Running

### Direct

```bash
source venv/bin/activate
set -a; source config.env; set +a
python gate.py
```

### As a systemd service

```bash
sudo systemctl start gate-scanner     # Start now
sudo systemctl status gate-scanner    # Check status
journalctl -u gate-scanner -f         # Live logs
```

Press **Esc** or **Alt+F4** to exit fullscreen (development only).

---

## Project Structure

```
gate-scanner/
├── gate.py                 # Main application (GUI + RFID processor + gate control)
├── requirements.txt        # Base Python dependencies
├── requirements-rpi.txt    # RPi-specific hardware deps (spidev, mfrc522)
├── config.env.example      # Environment variable template
├── gate-scanner.service    # systemd unit file for auto-start
├── install.sh              # One-command installer for RPi OS
├── .gitignore              # Git ignore rules
├── LICENSE                 # MIT License
├── CHANGELOG.md            # Version history
├── README.md               # This file
└── tests/
    ├── __init__.py
    └── test_gate.py        # Unit tests (pytest)
```

---

## Arduino Protocol

The RPi communicates with the Arduino Mega over USB serial using a simple text protocol. Each command is a line in `TYPE:VALUE\n` format:

| Command | Action |
|---|---|
| `GATE:OPEN` | Open the gate (unlock solenoid) |
| `GATE:CLOSE` | Close the gate (lock solenoid) |

The Arduino replies with standard acknowledgments like `Gate opened` or `Gate closed`.

### Access Granted Sequence

```
RPi → GATE:OPEN
     (wait GATE_OPEN_DURATION seconds)
RPi → GATE:CLOSE
```

### Access Denied Sequence

*No gate action. Display updates only.*

---

## API Contract

### Request

```http
POST /api/v1/gate/check-access
Content-Type: application/json
Authorization: Bearer <GATE_API_KEY>

{
    "card_uid": "0x1A2B3C4D"
}
```

### Response — Access Granted (200)

```json
{
    "data": {
        "allowed": true,
        "student": {
            "name": "أحمد محمد",
            "seat_number": "12345",
            "college": "engineering",
            "department": "information-technology",
            "photo": "/storage/students/photo.jpg"
        }
    }
}
```

### Response — Access Denied (200)

```json
{
    "data": {
        "allowed": false,
        "student": null
    }
}
```

---

## Health Check Endpoint

When `GATE_HEALTH_PORT` is set to a non-zero port, an HTTP server exposes:

```
GET http://localhost:<port>/health
```

### Response

```json
{
  "status": "ok",
  "arduino_connected": true,
  "rfid_reader_type": "RC522",
  "rfid_available": true,
  "uptime_seconds": 3600.5,
  "last_scan_ago_seconds": 12.3,
  "last_api_success_ago_seconds": 12.3,
  "total_scans": 142,
  "total_granted": 130,
  "total_denied": 12
}
```

---

## Security Features

| Feature | Description |
|---|---|
| **HMAC UID Hashing** | Raw UIDs are transformed into HMAC-SHA256 hashes immediately. Raw UIDs are never stored in logs or caches. |
| **File Permissions** | `config.env` containing the `UID_HASH_KEY` must have 600 permissions. |
| **API response validation** | `validate_api_response()` checks structure, types, and required fields. Malformed data is logged and rejected. |
| **SSL/TLS** | SSL verification enabled by default. Certificate pinning via `GATE_API_CERT_PATH`. |
| **No raw UID on screen** | Denied entries show a generic Arabic message, never the scanned card UID. |
| **API key as Bearer token** | Sent via `Authorization` header, not in query params or body. |

---

## Offline Fallback

When `GATE_OFFLINE_MODE=true`:

1. Every successful API check is cached locally (keyed by HMAC-SHA256 hash of the UID).
2. If the API becomes unreachable (all retries exhausted), the system checks the local cache.
3. If a valid (non-expired) cache entry exists, access is **granted** and logged as an offline hit.
4. Cache entries expire after `GATE_OFFLINE_CACHE_TTL` seconds (default: 300 = 5 minutes).

---

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=gate --cov-report=term-missing
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| **`GATE_API_URL is required`** | Set `GATE_API_URL` in `config.env`. The system no longer has a hardcoded default. |
| **`UID_HASH_KEY is required`** | Generate a 32-byte hex key (`openssl rand -hex 32`) and add it to `config.env` for secure UID hashing. |
| **Arduino not connecting** | Check `GATE_SERIAL_PORT` matches `ls /dev/ttyACM*`. The system auto-reconnects — check the header indicator. |
| **Arduino indicator red** | Serial connection lost. Check cable/port. Auto-reconnection runs with exponential backoff. |
| **RFID not scanning** | Ensure SPI is enabled in `raspi-config`. Verify wiring matches the RC522 table exactly. |
| **Arabic text garbled** | Install the Amiri font: `sudo apt-get install fonts-amiri` |
| **GUI not showing** | Ensure `DISPLAY=:0` is set. For SSH: `export DISPLAY=:0` or run via systemd. |
| **API errors / retries** | Check `~/gate-scanner.log`. Verify `GATE_API_URL`, network, and API key. |
| **Gate stays open** | Check serial connection and logs. The auto-close timer sends `GATE:CLOSE` after `GATE_OPEN_DURATION`. |

---

## License

MIT License — see [LICENSE](LICENSE) for details.
