# Changelog

All notable changes to the Gate Access Monitoring System are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

---

## [2.0.0] — 2026-03-05

### 🔴 BREAKING CHANGES
- **`GATE_API_URL` is now required.** The system exits with a clear error message if this environment variable is not set. The previously hardcoded production URL (`https://batu-gate.abdullah.top/…`) has been removed as a default for security reasons.

### Added

#### Security (URGENT)
- **Barcode input validation** — New `validate_barcode()` function enforces a 50-character max length, alphanumeric-only character whitelist (`A-Z`, `0-9`, `-`, `_`, `.`), and rejects empty input. Invalid barcodes are logged at WARNING level for security monitoring.
- **API response validation** — New `validate_api_response()` function validates the structure, types, and required fields of API responses. Malformed responses raise `ValidationError` and are logged.
- **SSL/TLS certificate pinning** — New `GATE_API_CERT_PATH` env var allows specifying a CA bundle for certificate pinning. `GATE_VERIFY_SSL` controls SSL verification (default: `true`). SSL errors are logged and never retried.
- **Gate operation feedback** — New `GateStatus` enum (`UNKNOWN`, `OPENING`, `OPEN`, `CLOSING`, `CLOSED`, `ERROR`, `OCCUPIED`) tracks the physical gate state. Arduino `GATE_STATUS:OCCUPIED/CLEAR` messages are parsed in real-time. Timeout-based OPEN confirmation check added to `grant_access_sequence()`.

#### Reliability (MORE IMPORTANT)
- **Arduino auto-reconnection** — New `GateController` class with a background thread that automatically reconnects with exponential backoff (2s → 60s cap) when the serial connection is lost. Send failures also trigger reconnection.
- **Arduino connection indicator** — Green/red dot in the GUI header shows real-time Arduino connection status.
- **API retry mechanism** — Exponential backoff retry for transient failures (timeout, 5xx, `ConnectionError`). Configurable via `GATE_API_MAX_RETRIES` (default: 3) and `GATE_API_RETRY_DELAY` (default: 1s). Non-retryable errors (4xx, SSL) fail immediately.
- **Thread-safe debounce** — `threading.Lock` now protects all access to the debounce dictionary. Previously it was accessed from multiple threads without synchronisation.
- **Offline fallback cache** — Optional time-limited local cache (`GATE_OFFLINE_MODE`, `GATE_OFFLINE_CACHE_TTL`). When enabled, recently-verified students can pass through during API outages. Barcodes are SHA-256 hashed (never stored raw).

#### Architecture (IMPORTANT)
- **`GateConfig` dataclass** — All configuration consolidated into a frozen dataclass with `from_env()` factory method and startup validation. Replaces scattered global variables.
- **`GateController` class** — Encapsulates Arduino serial connection, command sending, status reading, reconnection, and shutdown. Replaces bare global functions and `_arduino` global.
- **Custom exception hierarchy** — `GateError`, `ArduinoError`, `APIError`, `ValidationError` for structured error handling.
- **Health check HTTP server** — Optional `/health` endpoint (set `GATE_HEALTH_PORT`) returns JSON with Arduino status, gate status, uptime, scan counters, and last-scan timestamps.
- **Unit test suite** — New `tests/test_gate.py` with pytest tests covering barcode validation, API response validation, offline cache, Arabic reshaping, photo URL resolution, configuration loading, and exception hierarchy.

#### Files
- **`LICENSE`** — MIT License file (was referenced in README but missing).
- **`requirements-dev.txt`** — Development dependencies (pytest, mypy, pylint).
- **`CHANGELOG.md`** — This file.
- **`tests/__init__.py`** + **`tests/test_gate.py`** — Unit test suite.

### Changed
- **`requirements.txt`** — Added upper-bound version pins (e.g., `customtkinter>=5.2.0,<6.0`).
- **`config.env.example`** — Added 8 new environment variables with documentation.
- **`README.md`** — Comprehensive rewrite with new sections: Health Check, Security Features, Offline Fallback, Testing, expanded Configuration table, Retry Policy table, updated Troubleshooting.
- **Logging granularity** — Arduino commands logged at DEBUG, scans at INFO, failures at WARNING/ERROR. Third-party loggers (urllib3, PIL) suppressed.
- **Magic numbers extracted** — All timing values, sizes, and limits defined as named module-level constants (`ARDUINO_CONNECT_DELAY`, `GATE_CLOSE_DELAY`, `DENY_SEQUENCE_DURATION`, `BARCODE_MAX_LENGTH`, etc.).
- **Consistent type hints** — All functions, methods, and class attributes have explicit type annotations. `Callable`, `Optional`, `Any` used throughout.
- **`resolve_photo_url()`** now takes `base` as an explicit parameter instead of reading a global.

### Fixed
- **Debounce race condition** — Concurrent threads could corrupt the debounce dictionary. Now protected by `threading.Lock`.
- **Gate never confirmed OPEN** — The system now checks for Arduino OPEN acknowledgement with a configurable timeout.
- **Silent API failures** — API errors, malformed responses, and SSL issues are now explicitly handled and logged.
- **Hardcoded production URL removed** — No more default pointing to live infrastructure.

---

## [1.0.0] — 2025-12-01

Initial release.

- Fullscreen CustomTkinter kiosk GUI with Arabic RTL support.
- USB barcode scanning (HID keyboard mode or serial).
- REST API verification with Bearer token auth.
- Arduino gate control via `TYPE:VALUE` serial protocol.
- Student photo display with async download.
- Debounce for duplicate scans.
- Auto-close gate after configurable duration.
- History row showing last 3 entries.
- Rotating log file (`~/gate-scanner.log`).
- systemd service for auto-start.
- Bash installer for Raspberry Pi OS.
