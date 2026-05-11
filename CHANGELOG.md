# Changelog

All notable changes to the Gate Access Monitoring System are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

---

## [3.0.0] — 2026-05-09

### ⚠️ Breaking Changes

- **Arduino protocol changed**: The serial commands are now plain `OPEN\n` and `CLOSE\n`. Previous firmware using `GATE:OPEN` / `GATE:CLOSE` must be updated.
- **`GATE_API_URL` is now optional**: Defaults to the production server (`https://batu-gate.alnzam.online/api/v1/gate/check-access`). Override by setting the env var.
- **API payload field reverted**: The card identifier is sent as `bar_code`, matching the legacy barcode system, avoiding backend changes.
- **MIFARE Smart Card Provisioning**: Introduced `write_card.py` to write 7-digit student IDs directly into Sector 1 Block 4 of MIFARE 1K cards.
- **Smart Card Auto-Detection**: `RC522Reader` now reads programmed memory blocks and falls back to raw UID automatically.

### Added

- **RFID/NFC support** — Replaced USB barcode scanner with RC522 RFID module (SPI). Supports MIFARE 1K Classic cards. PN532 support planned for a future release.
- **`write_card.py` tool** — Administrator utility to program blank cards.
- **`HighThroughputProcessor`** — Producer-consumer thread pipeline decouples RFID polling from the GUI thread. Supports 6,000+ students/day (8–13 scans/min peak) with <1.5 s end-to-end latency.
- **`RFIDReaderBase` strategy pattern** — Swappable reader backends: `RC522Reader`, `PN532Reader` (stub), `SimulationReader`.
- **Simulation mode** — `SIMULATION_MODE=true` generates fake UIDs at configurable intervals (default 3 s) with a realistic scenario mix (80 % granted, 15 % denied, 5 % error). All simulated reads are marked `[SIMULATION]` in logs.
- **Rolling latency metrics** — `HighThroughputProcessor` tracks a 100-sample rolling average of end-to-end scan latency.
- **`CardType` enum** — Detects MIFARE 1K, 4K, and Ultralight from the SAK byte.
- **`RFIDCard` dataclass** — Typed container for UID, bytes, card type, ATQA, SAK, and read timestamp.
- **Built-in URL defaults** — Both `GATE_API_URL` and `BASE_MEDIA_URL` default to the production server so the system works out-of-the-box without a `config.env` file.

### Changed

- **Arduino Mega replaces ATmega** — Arduino Mega is the solenoid relay controller. All "smart" logic stays on the Raspberry Pi.
- **`GateController`** simplified to a minimal solenoid relay interface. Serial protocol reduced to `OPEN\n` and `CLOSE\n`.
- **`deny_access_sequence()`** — Removed the redundant background thread. Gate does not perform any hardware action on denied scans.
- **`GATE_API_URL`** — Now has a built-in default. No longer required.
- **`UID_HASH_KEY`** — Now has a dev-safe default. **Must be replaced** on the Raspberry Pi with `openssl rand -hex 32`.
- **`GATE_API_UID_FIELD`** env var renamed from `API_UID_FIELD` to `GATE_API_UID_FIELD` (consistent prefix).
- **`GATE_API_POOL_SIZE`** env var renamed from `API_POOL_SIZE` to `GATE_API_POOL_SIZE` (consistent prefix).
- **Baud rate default** changed from 115200 to 9600 to match the real Arduino sketch.
- **Config table in README** — `GATE_API_URL` and `UID_HASH_KEY` marked as optional (have defaults).

### Removed

- **USB Barcode Scanner as primary input** — Replaced by RC522 RFID module. Barcode scanner remains as an alternative input method only.
- **`validate_barcode()` function** — Replaced by `UIDValidator` class.
- **`DENY_SEQUENCE_DURATION` constant** — Was dead code after hardware removal.
- **`BARCODE_MAX_LENGTH` constant** — Removed with barcode validation.
- **`GateStatus.OCCUPIED`** — Removed along with the IR occupancy sensor.
- **`truncate_gate.py`** — Temporary one-off maintenance script removed from the repository.

### Fixed

- **`GATE_API_UID_FIELD` env var was silently ignored** — Was read with the wrong key (`API_UID_FIELD`), so the field name could never be overridden.
- **`GATE_API_POOL_SIZE` env var was silently ignored** — Same typo issue (`API_POOL_SIZE`).
- **`BASE_MEDIA_URL` defaulted to empty string** — Student photos would fail to load if the env var was not set. Now defaults to the real server URL.

---

## [2.0.0] — 2026-03-05

### 🔴 Breaking Changes

- **`GATE_API_URL` was required.** The system would exit with a clear error message if this environment variable was not set. *(Reverted in 3.0.0 — now has a default.)*

### Added

- **API response validation** — `validate_api_response()` validates the structure, types, and required fields of API responses.
- **SSL/TLS certificate pinning** — `GATE_API_CERT_PATH` env var, `GATE_VERIFY_SSL` flag.
- **`GateStatus` enum** — Tracks physical gate state.
- **Arduino auto-reconnection** — Background thread with exponential backoff (2 s → 60 s cap).
- **Arduino connection indicator** — Green/red dot in the GUI header.
- **API retry mechanism** — Exponential backoff for transient failures.
- **Offline fallback cache** — `GATE_OFFLINE_MODE`, `GATE_OFFLINE_CACHE_TTL`.
- **`GateConfig` dataclass** — Frozen dataclass with `from_env()` factory.
- **`GateController` class** — Encapsulates Arduino serial connection and command sending.
- **Health check HTTP server** — Optional `/health` endpoint (`GATE_HEALTH_PORT`).
- **Unit test suite** — `tests/test_gate.py` with pytest.

### Changed

- **`requirements.txt`** — Added upper-bound version pins.
- **`config.env.example`** — Added environment variables with documentation.
- **README** — Comprehensive rewrite.

### Fixed

- **Debounce race condition** — `threading.Lock` now protects the debounce dictionary.
- **Gate never confirmed OPEN** — Timeout-based OPEN confirmation check added.
- **Silent API failures** — All errors now explicitly handled and logged.

---

## [1.0.0] — 2025-12-01

Initial release.

- Fullscreen CustomTkinter kiosk GUI with Arabic RTL support.
- USB barcode scanning (HID keyboard mode or serial).
- REST API verification with Bearer token auth.
- Arduino gate control via serial protocol.
- Student photo display with async download.
- Debounce for duplicate scans.
- Auto-close gate after configurable duration.
- History row showing last 3 entries.
- Rotating log file (`~/gate-scanner.log`).
- systemd service for auto-start.
- Bash installer for Raspberry Pi OS.
