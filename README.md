<p align="center">
  <h1 align="center">🏛️ Gate Access Monitoring System</h1>
  <p align="center">
    <strong>BATU University — Smart Turnstile Gate Controller</strong><br>
    A fullscreen kiosk application running on Raspberry Pi 4B that verifies student IDs<br>
    via barcode scan, controls a physical turnstile gate, and displays student information on a monitor.
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

1. **Barcode Scanner** reads the student's ID barcode.
2. **Raspberry Pi 4B** validates the barcode input, then sends it to a remote REST API for verification (with automatic retry on transient failures).
3. If **access is granted**:
   - The monitor displays the student's photo, name, seat number, college, and department with a green "Access Granted" badge.
   - The Arduino opens the solenoid lock + servo gate, flashes a green LED, and beeps.
   - After a configurable delay, the gate auto-closes and re-locks.
   - The result is cached locally for offline fallback (if enabled).
4. If **access is denied**:
   - The monitor shows a red "Access Denied" badge (never exposing the raw barcode).
   - The Arduino flashes a red LED and beeps.
5. Previous scans are shown in a history row at the bottom of the screen.
6. If the API is unreachable and offline mode is enabled, recently-verified students can still pass through.

---

## Hardware Setup

| Component | Role | Connection |
|---|---|---|
| **Raspberry Pi 4B** | Main controller + display driver | HDMI to monitor |
| **ATmega / ESP32** | Gate actuator (solenoid, servo, LEDs, buzzers) | USB Serial (`/dev/ttyACM0`) |
| **USB Barcode Scanner** | Reads student ID barcodes | USB HID (keyboard mode) or serial |
| **HDMI Monitor** | Displays the kiosk GUI | HDMI |
| **Solenoid Lock** | Electromagnetic gate lock | Controlled via relay by Arduino |
| **Servo Motor** | Physical gate arm | Controlled via PCA9685 by Arduino |
| **Green / Red LEDs** | Visual access feedback | GPIO pins on Arduino |
| **Buzzers** | Audio access feedback | Shared with LED pins |
| **IR Sensor** | Gate occupancy detection | GPIO pin on Arduino |

### Wiring Diagram (Arduino Side)

```
Pin 27  →  Solenoid Relay (Active HIGH)
Pin 26  →  Servo Relay (Active LOW)
Pin 33  →  IR Sensor
Pin 14  →  Green LED + Buzzer
Pin 32  →  Red LED + Buzzer
I2C     →  PCA9685 Servo Driver
```

---

## Software Architecture

```
┌──────────────┐     USB Serial      ┌──────────────────┐
│  ATmega /    │◄────────────────────►│  Raspberry Pi 4B │
│  ESP32       │  GATE:OPEN/CLOSE    │                  │
│  (gate HW)   │  LED:GREEN/RED/OFF  │  gate.py         │
│              │  BUZZER:GREEN/RED   │  (CustomTkinter) │
└──────────────┘  GATE_STATUS:*      │                  │
                                     │   ┌──────────┐   │
  ┌──────────────┐   USB HID / Serial│   │  Monitor  │   │
  │  Barcode     │──────────────────►│   │  (HDMI)   │   │
  │  Scanner     │    stdin or serial│   └──────────┘   │
  └──────────────┘                   │                  │
                                     │  ┌────────────┐  │
                                     │  │ REST API   │  │
                                     │  │ (HTTPS)    │  │
                                     │  └────────────┘  │
                                     │                  │
                                     │  ┌────────────┐  │
                                     │  │ /health    │  │
                                     │  │ (HTTP)     │  │
                                     │  └────────────┘  │
                                     └──────────────────┘
```

### Key Design Decisions

| Concern | Solution |
|---|---|
| **Thread safety** | All Tkinter widget updates via `self.after(0, …)`. Debounce dict protected by `threading.Lock`. |
| **Input validation** | `validate_barcode()` enforces max-length and character whitelist before API calls. |
| **API resilience** | Exponential-backoff retry for transient errors (timeout, 5xx). SSL errors and 4xx fail immediately. |
| **SSL security** | Configurable certificate pinning via `GATE_API_CERT_PATH`. SSL verification enabled by default. |
| **Arduino reconnection** | Background thread with exponential backoff (2 s → 60 s cap) auto-reconnects on serial loss. |
| **Gate feedback** | `GateStatus` enum tracks gate state. Arduino `GATE_STATUS:*` messages are parsed in real-time. |
| **Duplicate scans** | Configurable debounce window rejects the same barcode within N seconds. |
| **Gate auto-close** | Timer sends `GATE:CLOSE` + `LED:OFF` after `GATE_OPEN_DURATION` seconds. |
| **Offline fallback** | Optional time-limited cache of successful checks (SHA-256 hashed barcodes). |
| **History management** | Entries stored as plain dicts — never extracted from widget text. |
| **Denied display** | Generic Arabic message shown; raw barcode never appears on screen. |
| **Logging** | Python `logging` with `RotatingFileHandler` (2 MB × 5 backups). DEBUG for Arduino, INFO for scans, WARNING for failures. |
| **Configuration** | All values via environment variables with startup validation. `GATE_API_URL` is *required*. |
| **Health monitoring** | Optional HTTP `/health` endpoint for external monitoring tools. |
| **Graceful shutdown** | On exit: `GATE:CLOSE` + `LED:OFF` + serial close. |
| **Global state** | Configuration in frozen `GateConfig` dataclass. Arduino connection in `GateController` class. |

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
3. Install Python dependencies from `requirements.txt`.
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

# Config
cp config.env.example config.env
nano config.env
```

### Development Setup

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
mypy gate.py --ignore-missing-imports
```

---

## Configuration

All settings are controlled via environment variables. Copy and edit the template:

```bash
cp config.env.example config.env
nano config.env
```

| Variable | Default | Required | Description |
|---|---|:---:|---|
| `GATE_API_URL` | *(none)* | **Yes** | REST API endpoint for access checks |
| `GATE_API_KEY` | *(empty)* | No | Bearer token for API authentication |
| `GATE_API_TIMEOUT` | `8` | No | API request timeout (seconds) |
| `GATE_API_MAX_RETRIES` | `3` | No | Max retry attempts for transient failures |
| `GATE_API_RETRY_DELAY` | `1` | No | Base delay between retries (exponential backoff) |
| `GATE_VERIFY_SSL` | `true` | No | Enable/disable SSL certificate verification |
| `GATE_API_CERT_PATH` | *(empty)* | No | Path to CA bundle for certificate pinning |
| `GATE_SERIAL_PORT` | `/dev/ttyACM0` | No | Arduino serial port |
| `GATE_BAUD_RATE` | `115200` | No | Arduino baud rate |
| `BARCODE_SERIAL_PORT` | *(empty = stdin)* | No | Barcode scanner serial port |
| `BARCODE_BAUD_RATE` | `9600` | No | Barcode scanner baud rate |
| `GATE_OPEN_DURATION` | `5` | No | Seconds gate stays open |
| `DEBOUNCE_SECONDS` | `3` | No | Duplicate scan ignore window |
| `BASE_MEDIA_URL` | *(empty)* | No | Photo URL prefix for relative paths |
| `GATE_OFFLINE_MODE` | `false` | No | Enable offline cache fallback |
| `GATE_OFFLINE_CACHE_TTL` | `300` | No | Offline cache entry TTL (seconds) |
| `GATE_HEALTH_PORT` | `0` | No | Health check HTTP port (0 = disabled) |

> **Breaking change:** `GATE_API_URL` is now **required**. The system will refuse to start if it is not set. Previously, it defaulted to the production URL.

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
├── gate.py                 # Main application (kiosk GUI + scanner + gate control)
├── requirements.txt        # Python dependencies (pinned with upper bounds)
├── requirements-dev.txt    # Development dependencies (pytest, mypy, pylint)
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

The RPi communicates with the ATmega/ESP32 over USB serial using a simple text protocol. Each command is a line in `TYPE:VALUE\n` format:

| Command | Action |
|---|---|
| `GATE:OPEN` | Unlock solenoid → open servo to 120° |
| `GATE:CLOSE` | Close servo to 0° → lock solenoid |
| `LED:GREEN` | Green LED on, red LED off |
| `LED:RED` | Red LED on, green LED off |
| `LED:OFF` | All LEDs off |
| `BUZZER:GREEN` | Short green buzzer beep |
| `BUZZER:RED` | Short red buzzer beep |

The Arduino replies with acknowledgments like `Gate opened`, `Red LED on`, etc.
It also sends periodic `GATE_STATUS:OCCUPIED` or `GATE_STATUS:CLEAR` from the IR sensor.

### Access Granted Sequence

```
RPi → LED:GREEN
RPi → BUZZER:GREEN
RPi → GATE:OPEN
     (wait for OPEN confirmation or 3s timeout)
     (wait GATE_OPEN_DURATION seconds)
RPi → GATE:CLOSE
RPi → LED:OFF
```

### Access Denied Sequence

```
RPi → LED:RED
RPi → BUZZER:RED
     (wait 2 seconds)
RPi → LED:OFF
```

---

## API Contract

### Request

```http
POST /api/v1/gate/check-access
Content-Type: application/json
Authorization: Bearer <GATE_API_KEY>

{
    "bar_code": "1234567890"
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

### Retry Policy

| Error Type | Retried? | Notes |
|---|:---:|---|
| Timeout | Yes | Up to `GATE_API_MAX_RETRIES` with exponential backoff |
| Connection error | Yes | Network unreachable, DNS failure |
| HTTP 5xx | Yes | Server error |
| HTTP 4xx | No | Client error (bad request, unauthorized) |
| SSL error | No | Certificate verification failure |
| Malformed JSON | No | Invalid response structure |

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
  "gate_status": "closed",
  "uptime_seconds": 3600.5,
  "last_scan_ago_seconds": 12.3,
  "last_api_success_ago_seconds": 12.3,
  "total_scans": 142,
  "total_granted": 130,
  "total_denied": 12
}
```

Use this with monitoring tools like Prometheus (with a JSON exporter), Uptime Kuma, or a simple cron-based health check script.

---

## Security Features

| Feature | Description |
|---|---|
| **Barcode validation** | Max 50 chars, alphanumeric + `_-.` only. SQL injection and XSS payloads are rejected and logged. |
| **API response validation** | `validate_api_response()` checks structure, types, and required fields. Malformed data is logged and rejected. |
| **SSL/TLS** | SSL verification enabled by default. Certificate pinning via `GATE_API_CERT_PATH`. |
| **No raw barcode on screen** | Denied entries show a generic Arabic message, never the scanned barcode. |
| **API key as Bearer token** | Sent via `Authorization` header, not in query params or body. |
| **Hashed offline cache** | Barcodes are SHA-256 hashed before caching — raw barcodes are never stored. |
| **Security logging** | Rejected barcodes are logged at WARNING level with details for incident review. |
| **Mandatory API URL** | No hardcoded production URL — must be explicitly configured. |

---

## Offline Fallback

When `GATE_OFFLINE_MODE=true`:

1. Every successful API check is cached locally (keyed by SHA-256 hash of the barcode).
2. If the API becomes unreachable (all retries exhausted), the system checks the local cache.
3. If a valid (non-expired) cache entry exists, access is **granted** and logged as an offline hit.
4. Cache entries expire after `GATE_OFFLINE_CACHE_TTL` seconds (default: 300 = 5 minutes).
5. All offline-granted access is logged with the barcode hash for auditing.

> **Note:** Offline mode is disabled by default. Enable it only if brief API outages should not block all access.

---

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=gate --cov-report=term-missing

# Type checking
mypy gate.py --ignore-missing-imports

# Linting
pylint gate.py
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| **`GATE_API_URL is required`** | Set `GATE_API_URL` in `config.env`. The system no longer has a hardcoded default. |
| **Arduino not connecting** | Check `GATE_SERIAL_PORT` matches `ls /dev/ttyACM*`. The system auto-reconnects — check the header indicator. |
| **Arduino indicator red** | Serial connection lost. Check cable/port. Auto-reconnection runs with exponential backoff. |
| **Barcode not scanning** | If HID mode: ensure terminal has focus. If serial: check `BARCODE_SERIAL_PORT`. |
| **Barcode rejected (SECURITY log)** | The barcode contains invalid characters or exceeds 50 chars. Check `~/gate-scanner.log`. |
| **Arabic text garbled** | Install the Amiri font: `sudo apt-get install fonts-amiri` |
| **GUI not showing** | Ensure `DISPLAY=:0` is set. For SSH: `export DISPLAY=:0` or run via systemd. |
| **API errors / retries** | Check `~/gate-scanner.log`. Verify `GATE_API_URL`, network, and API key. |
| **SSL certificate error** | Verify the cert at `GATE_API_CERT_PATH` exists and is valid. Use `GATE_VERIFY_SSL=false` only for testing. |
| **Gate stays open** | Check serial connection and logs. The auto-close timer sends `GATE:CLOSE` after `GATE_OPEN_DURATION`. |
| **Permission denied on serial** | `sudo usermod -aG dialout $USER && reboot` |
| **Health check not responding** | Ensure `GATE_HEALTH_PORT` is set and the port is not already in use. |

### Log Files

```bash
# Application log (with rotation, max 2 MB × 5 backups)
tail -f ~/gate-scanner.log

# systemd journal
journalctl -u gate-scanner -f --no-pager
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.
