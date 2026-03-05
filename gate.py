#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate Access Monitoring System — BATU University
================================================

A fullscreen kiosk application for the Raspberry Pi 4B that controls a
university turnstile gate.  It reads student barcodes, verifies them against
a remote REST API, and displays student information on an attached monitor.

Hardware
--------
- **Raspberry Pi 4B** — runs this script and the display.
- **ATmega / Arduino** — controls solenoid lock, servo, LEDs, and buzzers
  via USB-serial.  Accepts commands in ``TYPE:VALUE`` format
  (e.g. ``GATE:OPEN``, ``LED:GREEN``, ``BUZZER:RED``).
- **USB Barcode Scanner** — HID keyboard mode (stdin) or dedicated serial.
- **Monitor** — connected via HDMI; shows the CustomTkinter GUI.

Environment Variables (see ``config.env.example`` for full list)
-----------------------------------------------------------------
``GATE_API_URL``            API endpoint for access checks (**required**).
``GATE_API_KEY``            Bearer token sent as ``Authorization`` header.
``GATE_API_CERT_PATH``      Path to CA bundle for SSL certificate pinning.
``GATE_VERIFY_SSL``         Set ``false`` to disable SSL verification.
``GATE_API_TIMEOUT``        API request timeout in seconds (default ``8``).
``GATE_API_MAX_RETRIES``    API retry attempts (default ``3``).
``GATE_API_RETRY_DELAY``    Base delay between retries (default ``1``).
``GATE_SERIAL_PORT``        Arduino serial port (default ``/dev/ttyACM0``).
``GATE_BAUD_RATE``          Arduino baud rate  (default ``115200``).
``BARCODE_SERIAL_PORT``     Serial port for barcode scanner (empty = stdin).
``BARCODE_BAUD_RATE``       Barcode scanner baud rate (default ``9600``).
``GATE_OPEN_DURATION``      Seconds the gate stays open (default ``5``).
``DEBOUNCE_SECONDS``        Duplicate scan ignore window (default ``3``).
``BASE_MEDIA_URL``          Prefix for relative photo URLs.
``GATE_OFFLINE_MODE``       Enable offline cache fallback (default ``false``).
``GATE_OFFLINE_CACHE_TTL``  Offline cache TTL in seconds (default ``300``).
``GATE_HEALTH_PORT``        Health check HTTP port (default ``0`` = off).

Author : Abdullah
License: MIT
"""

# ═══════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ═══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import enum
import hashlib
import http.server
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any, Callable, Optional

import arabic_reshaper
import customtkinter as ctk
import requests
import serial
from bidi.algorithm import get_display
from PIL import Image, ImageTk

# ═══════════════════════════════════════════════════════════════════════════════
# CUSTOM EXCEPTIONS
# ═══════════════════════════════════════════════════════════════════════════════


class GateError(Exception):
    """Base exception for all gate-system errors."""


class ArduinoError(GateError):
    """Raised when the Arduino serial connection fails."""


class APIError(GateError):
    """Raised when the access-check API returns an invalid response."""


class ValidationError(GateError):
    """Raised when input validation fails (barcode, API response, etc.)."""


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING — file + console, with log rotation
# ═══════════════════════════════════════════════════════════════════════════════

LOG_DIR = os.path.expanduser("~")
LOG_FILE = os.path.join(LOG_DIR, "gate-scanner.log")

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_console_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  [%(levelname)-7s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_file_handler, _console_handler],
)

# Keep third-party loggers quiet
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)

log = logging.getLogger("gate")

# ═══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL CONSTANTS (non-configurable timing / sizing)
# ═══════════════════════════════════════════════════════════════════════════════

ARDUINO_CONNECT_DELAY: float = 2.0      # seconds after opening serial
GATE_CLOSE_DELAY: float = 0.3           # brief pause after GATE:CLOSE
DENY_SEQUENCE_DURATION: float = 2.0     # seconds LED:RED stays on
STATUS_BAR_RESET_MS: int = 4000         # ms before status bar resets
CLOCK_UPDATE_MS: int = 1000             # ms between clock ticks
INDICATOR_UPDATE_MS: int = 2000         # ms between Arduino indicator checks
ARDUINO_RECONNECT_MIN: float = 2.0      # starting reconnect delay
ARDUINO_RECONNECT_MAX: float = 60.0     # max reconnect delay (cap)
GATE_OPEN_CONFIRM_TIMEOUT: float = 3.0  # seconds to wait for OPEN ack
PHOTO_FETCH_TIMEOUT: float = 8.0        # seconds for photo downloads

# Barcode validation
BARCODE_MAX_LENGTH: int = 50
_BARCODE_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9\-_.]+$")

# Photo widget dimensions (pixels)
MAIN_PHOTO_SIZE: tuple[int, int] = (400, 300)
SMALL_PHOTO_SIZE: tuple[int, int] = (240, 300)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — typed dataclass loaded from environment variables
# ═══════════════════════════════════════════════════════════════════════════════


def _bool_env(name: str, default: bool) -> bool:
    """Parse a boolean environment variable (true/false/1/0/yes/no)."""
    val = os.getenv(name, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class GateConfig:
    """Immutable application configuration loaded from environment variables.

    Use ``GateConfig.from_env()`` to construct an instance.
    All values are validated at construction time.
    """

    # ── API ───────────────────────────────────────────────────────────────────
    api_url: str
    api_key: str
    api_cert_path: str
    verify_ssl: bool
    api_timeout: float
    api_max_retries: int
    api_retry_delay: float

    # ── Arduino / gate controller ─────────────────────────────────────────────
    serial_port: str
    baud_rate: int

    # ── Barcode scanner ───────────────────────────────────────────────────────
    barcode_serial_port: str
    barcode_baud_rate: int

    # ── Gate timing ───────────────────────────────────────────────────────────
    gate_open_duration: float
    debounce_seconds: float

    # ── Media ─────────────────────────────────────────────────────────────────
    base_media_url: str

    # ── Offline cache ─────────────────────────────────────────────────────────
    offline_mode: bool
    offline_cache_ttl: float

    # ── Health check ──────────────────────────────────────────────────────────
    health_port: int

    # ── derived helpers ───────────────────────────────────────────────────────

    @property
    def ssl_verify(self) -> bool | str:
        """Return the value for ``requests``' ``verify`` parameter."""
        if not self.verify_ssl:
            return False
        if self.api_cert_path:
            return self.api_cert_path
        return True

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> GateConfig:
        """Construct a ``GateConfig`` from environment variables.

        Raises :class:`GateError` when critical values are missing or invalid.
        """
        api_url = os.getenv("GATE_API_URL", "").strip()
        if not api_url:
            raise GateError(
                "GATE_API_URL is required but not set.\n"
                "Set it in config.env or as an environment variable.\n"
                "Example: GATE_API_URL=https://your-server.com/api/v1/gate/check-access"
            )

        api_cert_path = os.getenv("GATE_API_CERT_PATH", "").strip()
        if api_cert_path and not os.path.isfile(api_cert_path):
            raise GateError(
                f"GATE_API_CERT_PATH points to a non-existent file: {api_cert_path}"
            )

        gate_open_duration = float(os.getenv("GATE_OPEN_DURATION", "5"))
        if gate_open_duration <= 0:
            raise GateError("GATE_OPEN_DURATION must be a positive number.")

        debounce_seconds = float(os.getenv("DEBOUNCE_SECONDS", "3"))
        if debounce_seconds < 0:
            raise GateError("DEBOUNCE_SECONDS must be non-negative.")

        return cls(
            api_url=api_url,
            api_key=os.getenv("GATE_API_KEY", "").strip(),
            api_cert_path=api_cert_path,
            verify_ssl=_bool_env("GATE_VERIFY_SSL", True),
            api_timeout=float(os.getenv("GATE_API_TIMEOUT", "8")),
            api_max_retries=int(os.getenv("GATE_API_MAX_RETRIES", "3")),
            api_retry_delay=float(os.getenv("GATE_API_RETRY_DELAY", "1")),
            serial_port=os.getenv("GATE_SERIAL_PORT", "/dev/ttyACM0"),
            baud_rate=int(os.getenv("GATE_BAUD_RATE", "115200")),
            barcode_serial_port=os.getenv("BARCODE_SERIAL_PORT", "").strip(),
            barcode_baud_rate=int(os.getenv("BARCODE_BAUD_RATE", "9600")),
            gate_open_duration=gate_open_duration,
            debounce_seconds=debounce_seconds,
            base_media_url=os.getenv("BASE_MEDIA_URL", "").strip(),
            offline_mode=_bool_env("GATE_OFFLINE_MODE", False),
            offline_cache_ttl=float(os.getenv("GATE_OFFLINE_CACHE_TTL", "300")),
            health_port=int(os.getenv("GATE_HEALTH_PORT", "0")),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# APPEARANCE
# ═══════════════════════════════════════════════════════════════════════════════

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class Colors:
    """Centralised colour constants so nothing is hard-coded twice."""

    BG_DARK: str = "#0f172a"
    CARD_BG: str = "#1e293b"
    CARD_BORDER: str = "#3b82f6"
    HISTORY_BORDER: str = "#475569"
    PHOTO_BG: str = "#0f172a"
    HEADER_BG: str = "#1e40af"
    STATUS_BAR_BG: str = "#1e293b"
    GREEN: str = "#10b981"
    RED: str = "#ef4444"
    ORANGE: str = "#f59e0b"
    BLUE_TEXT: str = "#60a5fa"
    MUTED_TEXT: str = "#94a3b8"
    LIGHT_BLUE: str = "#93c5fd"
    TIME_BG: str = "#334155"
    DOT_IDLE: str = "#64748b"
    DOT_CLEAR: str = "#334155"


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSLATION TABLES — English slug → Arabic display name
# ═══════════════════════════════════════════════════════════════════════════════

COLLEGE_NAMES: dict[str, str] = {
    "industry-and-energy": "كلية الصناعة والطاقة",
    "engineering":         "كلية الهندسة",
    "science":             "كلية العلوم",
    "commerce":            "كلية التجارة",
}

DEPARTMENT_NAMES: dict[str, str] = {
    "information-technology": "تكنولوجيا المعلومات",
    "computer-science":       "علوم الحاسب",
    "electrical":             "هندسة كهربائية",
    "mechanical":             "هندسة ميكانيكية",
}

# ═══════════════════════════════════════════════════════════════════════════════
# GATE STATUS ENUM
# ═══════════════════════════════════════════════════════════════════════════════


class GateStatus(enum.Enum):
    """Physical state of the turnstile gate."""

    UNKNOWN = "unknown"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"
    OCCUPIED = "occupied"  # IR sensor triggered while gate is open


# ═══════════════════════════════════════════════════════════════════════════════
# GATE CONTROLLER — encapsulates Arduino serial communication
# ═══════════════════════════════════════════════════════════════════════════════


class GateController:
    """
    Manages the Arduino / ESP32 gate controller over USB serial.

    Features
    --------
    - Thread-safe command sending.
    - **Automatic reconnection** with exponential backoff when disconnected.
    - Background reader thread that parses Arduino status messages
      (``GATE_STATUS:OCCUPIED``, ``GATE_STATUS:CLEAR``, acknowledgements).
    - Gate state tracking via :class:`GateStatus`.
    - Graceful shutdown (sends ``GATE:CLOSE`` + ``LED:OFF``).

    The controller speaks the ``TYPE:VALUE\\n`` protocol::

        GATE:OPEN   — unlock solenoid + open servo
        GATE:CLOSE  — close servo + lock solenoid
        LED:GREEN   — green LED on (red off)
        LED:RED     — red LED on (green off)
        LED:OFF     — all LEDs off
        BUZZER:GREEN / BUZZER:RED — short beep
    """

    def __init__(self, config: GateConfig) -> None:
        self._config: GateConfig = config
        self._serial: Optional[serial.Serial] = None
        self._write_lock: threading.Lock = threading.Lock()
        self._is_connected: bool = False
        self._gate_status: GateStatus = GateStatus.UNKNOWN
        self._status_lock: threading.Lock = threading.Lock()
        self._shutdown: threading.Event = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None
        self._on_status_change: Optional[Callable[[GateStatus], None]] = None

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """Whether the Arduino serial link is currently open."""
        return self._is_connected

    @property
    def gate_status(self) -> GateStatus:
        """Current physical gate state (best-effort, based on commands + feedback)."""
        with self._status_lock:
            return self._gate_status

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Perform initial connection attempt and start the background thread."""
        self._try_connect()
        self._bg_thread = threading.Thread(
            target=self._background_loop, daemon=True, name="arduino-bg",
        )
        self._bg_thread.start()

    def shutdown(self) -> None:
        """Send safe-state commands and close the serial port."""
        log.info("GateController shutting down…")
        self._shutdown.set()
        self.send_command("GATE:CLOSE")
        self.send_command("LED:OFF")
        with self._write_lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._is_connected = False

    # ── public command API ────────────────────────────────────────────────────

    def send_command(self, cmd: str) -> bool:
        """
        Send a single ``TYPE:VALUE`` command to the Arduino (thread-safe).

        Returns ``True`` if the write succeeded, ``False`` otherwise.
        On failure the connection is marked as disconnected so the
        background thread will begin reconnection.
        """
        with self._write_lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.write(f"{cmd}\n".encode())
                    log.debug("→ Arduino: %s", cmd)
                    return True
                except serial.SerialException as exc:
                    log.error("Serial write failed (%s): %s", cmd, exc)
                    self._is_connected = False
                    return False
                except Exception as exc:
                    log.error("Unexpected serial error (%s): %s", cmd, exc)
                    return False
            else:
                log.warning("Arduino not connected — skipped: %s", cmd)
                return False

    def grant_access_sequence(self) -> None:
        """
        Full "access granted" hardware sequence (runs in a daemon thread):

        1. Green LED + buzzer beep
        2. Open gate  (with timeout-based OPEN confirmation check)
        3. Wait ``gate_open_duration`` seconds
        4. Close gate
        5. LEDs off
        """
        def _run() -> None:
            self._set_gate_status(GateStatus.OPENING)
            self.send_command("LED:GREEN")
            self.send_command("BUZZER:GREEN")

            ok = self.send_command("GATE:OPEN")
            if ok:
                # Wait briefly for the Arduino to acknowledge OPEN
                deadline = time.monotonic() + GATE_OPEN_CONFIRM_TIMEOUT
                while time.monotonic() < deadline:
                    if self.gate_status == GateStatus.OPEN:
                        break
                    time.sleep(0.1)
                else:
                    # Timeout — assume gate opened (Arduino may not send ack)
                    self._set_gate_status(GateStatus.OPEN)
                    log.debug("Gate OPEN not confirmed within timeout — proceeding")
            else:
                self._set_gate_status(GateStatus.ERROR)
                log.error("GATE:OPEN command failed — possible hardware issue")

            time.sleep(self._config.gate_open_duration)

            self._set_gate_status(GateStatus.CLOSING)
            self.send_command("GATE:CLOSE")
            time.sleep(GATE_CLOSE_DELAY)
            self.send_command("LED:OFF")
            self._set_gate_status(GateStatus.CLOSED)

        threading.Thread(target=_run, daemon=True, name="grant-seq").start()

    def deny_access_sequence(self) -> None:
        """
        Full "access denied" hardware sequence (runs in a daemon thread):

        Red LED + buzzer, wait 2 s, LEDs off.
        """
        def _run() -> None:
            self.send_command("LED:RED")
            self.send_command("BUZZER:RED")
            time.sleep(DENY_SEQUENCE_DURATION)
            self.send_command("LED:OFF")

        threading.Thread(target=_run, daemon=True, name="deny-seq").start()

    # ── background loop (reader + reconnection) ──────────────────────────────

    def _background_loop(self) -> None:
        """Combined serial reader and automatic reconnection loop."""
        reconnect_delay = ARDUINO_RECONNECT_MIN

        while not self._shutdown.is_set():
            # ── Phase 1: ensure connection ────────────────────────────────
            if not self._is_connected:
                if self._try_connect():
                    reconnect_delay = ARDUINO_RECONNECT_MIN
                else:
                    self._shutdown.wait(min(reconnect_delay, ARDUINO_RECONNECT_MAX))
                    reconnect_delay = min(reconnect_delay * 2, ARDUINO_RECONNECT_MAX)
                    continue

            # ── Phase 2: read status messages ─────────────────────────────
            ser = self._serial
            if not (ser and ser.is_open):
                self._mark_disconnected()
                continue

            try:
                line = ser.readline().decode(errors="ignore").strip()
                if line:
                    self._handle_arduino_message(line)
            except serial.SerialException:
                log.warning("Arduino disconnected during read")
                self._mark_disconnected()
            except Exception as exc:
                log.debug("Arduino read error: %s", exc)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _try_connect(self) -> bool:
        """Attempt to open the serial port. Returns ``True`` on success."""
        with self._write_lock:
            try:
                self._serial = serial.Serial(
                    self._config.serial_port,
                    self._config.baud_rate,
                    timeout=1,
                )
                time.sleep(ARDUINO_CONNECT_DELAY)
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
                greeting = self._serial.readline().decode(errors="ignore").strip()
                self._is_connected = True
                self._set_gate_status(GateStatus.UNKNOWN)
                log.info(
                    "Arduino connected on %s @ %d baud — %s",
                    self._config.serial_port,
                    self._config.baud_rate,
                    greeting or "(no greeting)",
                )
                return True
            except Exception as exc:
                log.debug("Arduino connect attempt failed: %s", exc)
                self._serial = None
                self._is_connected = False
                return False

    def _mark_disconnected(self) -> None:
        """Mark the connection as lost (safe to call multiple times)."""
        with self._write_lock:
            self._is_connected = False
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
        self._set_gate_status(GateStatus.ERROR)

    def _handle_arduino_message(self, msg: str) -> None:
        """Parse and act on a status message received from the Arduino."""
        log.debug("← Arduino: %s", msg)

        upper = msg.upper()
        if upper.startswith("GATE_STATUS:"):
            value = upper.split(":", 1)[1].strip()
            if value == "OCCUPIED":
                self._set_gate_status(GateStatus.OCCUPIED)
            elif value == "CLEAR":
                if self.gate_status == GateStatus.OCCUPIED:
                    self._set_gate_status(GateStatus.CLOSED)
        elif "opened" in msg.lower():
            self._set_gate_status(GateStatus.OPEN)
        elif "closed" in msg.lower():
            self._set_gate_status(GateStatus.CLOSED)

    def _set_gate_status(self, status: GateStatus) -> None:
        """Update gate status and fire the optional callback."""
        with self._status_lock:
            if self._gate_status == status:
                return
            self._gate_status = status
        log.debug("Gate status → %s", status.value)
        if self._on_status_change:
            try:
                self._on_status_change(status)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# BARCODE VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════


def validate_barcode(code: str) -> bool:
    """
    Validate a scanned barcode string.

    Rules
    -----
    - Must not be empty.
    - Maximum ``BARCODE_MAX_LENGTH`` characters (50).
    - Only alphanumeric characters, hyphens, underscores, dots.
    - No control characters or embedded whitespace.

    Rejected scans are logged at WARNING for security monitoring.

    Returns
    -------
    bool
        ``True`` if the barcode passes validation.
    """
    if not code:
        log.warning("SECURITY: empty barcode rejected")
        return False

    if len(code) > BARCODE_MAX_LENGTH:
        log.warning(
            "SECURITY: barcode exceeds max length (%d > %d): %s…",
            len(code), BARCODE_MAX_LENGTH, code[:20],
        )
        return False

    if not _BARCODE_PATTERN.match(code):
        log.warning("SECURITY: barcode contains invalid characters: %r", code[:50])
        return False

    return True


# ═══════════════════════════════════════════════════════════════════════════════
# API RESPONSE VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════


def validate_api_response(data: Any) -> tuple[bool, dict[str, Any]]:
    """
    Validate the structure of the access-check API response.

    Expected format::

        {"data": {"allowed": <bool>, "student": <dict|null>}}

    Parameters
    ----------
    data : Any
        The parsed JSON body from the API.

    Returns
    -------
    tuple[bool, dict]
        ``(allowed, student_dict)``.  ``student_dict`` is empty when denied.

    Raises
    ------
    ValidationError
        If the response shape is malformed.
    """
    if not isinstance(data, dict):
        raise ValidationError(f"Expected dict, got {type(data).__name__}")

    inner = data.get("data")
    if not isinstance(inner, dict):
        raise ValidationError("Missing or invalid 'data' key in API response")

    allowed = inner.get("allowed")
    if not isinstance(allowed, bool):
        raise ValidationError(
            f"'allowed' must be boolean, got {type(allowed).__name__}"
        )

    student = inner.get("student")
    if allowed and student is not None:
        if not isinstance(student, dict):
            raise ValidationError(
                f"'student' must be dict or null, got {type(student).__name__}"
            )
        # Validate required fields
        if "name" not in student:
            raise ValidationError("Student object missing required field: 'name'")

    return allowed, student if isinstance(student, dict) else {}


# ═══════════════════════════════════════════════════════════════════════════════
# OFFLINE CACHE
# ═══════════════════════════════════════════════════════════════════════════════


class OfflineCache:
    """
    Time-limited local cache of recent *successful* access checks.

    When enabled and the API is unreachable, the system can grant access
    to students who were recently verified.  All offline-granted entries
    are logged with the barcode hash for auditing.

    Thread-safe.
    """

    def __init__(self, ttl: float = 300.0, enabled: bool = False) -> None:
        self._ttl: float = ttl
        self._enabled: bool = enabled
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._lock: threading.Lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _key(barcode: str) -> str:
        """SHA-256 hash of the barcode (never store raw barcodes on disk)."""
        return hashlib.sha256(barcode.encode()).hexdigest()

    def store(self, barcode: str, student: dict[str, Any]) -> None:
        """Cache a successful access check result."""
        if not self._enabled:
            return
        key = self._key(barcode)
        with self._lock:
            self._cache[key] = (student, time.time())

    def lookup(self, barcode: str) -> Optional[dict[str, Any]]:
        """Return cached student data if still valid, or ``None``."""
        if not self._enabled:
            return None
        key = self._key(barcode)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            student, ts = entry
            if time.time() - ts <= self._ttl:
                return student
            del self._cache[key]
            return None

    def prune(self) -> None:
        """Remove expired entries from the cache."""
        if not self._enabled:
            return
        now = time.time()
        with self._lock:
            self._cache = {
                k: v for k, v in self._cache.items() if now - v[1] <= self._ttl
            }


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH STATE — lightweight metrics dict updated by various components
# ═══════════════════════════════════════════════════════════════════════════════
# CPython's GIL guarantees atomic reads/writes of individual dict values,
# so no lock is needed for these simple counters.

_health_state: dict[str, Any] = {
    "start_time": 0.0,
    "last_scan_time": 0.0,
    "last_api_success_time": 0.0,
    "total_scans": 0,
    "total_granted": 0,
    "total_denied": 0,
}


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECK HTTP SERVER
# ═══════════════════════════════════════════════════════════════════════════════


class HealthCheckServer:
    """
    Minimal HTTP server exposing ``/health`` for external monitoring tools.

    The JSON response includes:
    - Arduino connection status
    - Gate status
    - System uptime
    - Last scan time
    - Total scans / granted / denied counters

    Start with :meth:`start` (runs in a daemon thread).
    """

    def __init__(self, port: int, controller: GateController) -> None:
        self._port: int = port
        self._controller: GateController = controller
        self._server: Optional[http.server.HTTPServer] = None

    def start(self) -> None:
        """Bind to the configured port and serve in a daemon thread."""
        controller = self._controller

        class _Handler(http.server.BaseHTTPRequestHandler):
            """HTTP request handler for /health endpoint."""

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    now = time.monotonic()
                    body = json.dumps(
                        {
                            "status": "ok",
                            "arduino_connected": controller.connected,
                            "gate_status": controller.gate_status.value,
                            "uptime_seconds": round(
                                now - _health_state["start_time"], 1,
                            ),
                            "last_scan_ago_seconds": (
                                round(now - _health_state["last_scan_time"], 1)
                                if _health_state["last_scan_time"]
                                else None
                            ),
                            "last_api_success_ago_seconds": (
                                round(now - _health_state["last_api_success_time"], 1)
                                if _health_state["last_api_success_time"]
                                else None
                            ),
                            "total_scans": _health_state["total_scans"],
                            "total_granted": _health_state["total_granted"],
                            "total_denied": _health_state["total_denied"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body.encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                # Suppress default access-log noise
                pass

        try:
            self._server = http.server.HTTPServer(("", self._port), _Handler)
            thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="health-http",
            )
            thread.start()
            log.info("Health check server started on port %d", self._port)
        except OSError as exc:
            log.warning("Could not start health check server on port %d: %s",
                        self._port, exc)

    def stop(self) -> None:
        """Shut down the HTTP server (if running)."""
        if self._server:
            self._server.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# ARABIC TEXT HELPER
# ═══════════════════════════════════════════════════════════════════════════════


def reshape_arabic(text: str) -> str:
    """
    Reshape Arabic text so it displays correctly in Tkinter (which has
    no native RTL / shaping support).

    Returns the original string unchanged if reshaping fails.
    """
    if not text:
        return ""
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception as exc:
        log.debug("Arabic reshape failed for %r: %s", text, exc)
        return text


# ═══════════════════════════════════════════════════════════════════════════════
# PHOTO LOADING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _crop_and_resize(
    img: Image.Image, target: tuple[int, int],
) -> Image.Image:
    """Centre-crop *img* to the aspect ratio of *target*, then resize."""
    tw, th = target
    ratio = tw / th
    if img.width / img.height > ratio:
        nw = int(img.height * ratio)
        left = (img.width - nw) // 2
        img = img.crop((left, 0, left + nw, img.height))
    else:
        nh = int(img.width / ratio)
        top = (img.height - nh) // 2
        img = img.crop((0, top, img.width, top + nh))
    return img.resize(target, Image.Resampling.LANCZOS)


def load_photo_async(
    url: str,
    size: tuple[int, int],
    widget: ctk.CTkBaseClass,
    callback: Callable[[Image.Image], None],
) -> None:
    """
    Download an image in a daemon thread, crop/resize it, and schedule
    *callback(pil_image)* on the Tk main thread via ``widget.after(0, …)``.

    Skips localhost URLs to avoid hanging during development.
    """
    if not url or any(x in url.lower() for x in ("localhost", "127.0.0.1")):
        return

    def _worker() -> None:
        try:
            r = requests.get(url, timeout=PHOTO_FETCH_TIMEOUT, stream=True)
            r.raise_for_status()
            img = _crop_and_resize(
                Image.open(BytesIO(r.content)).convert("RGB"), size,
            )
            widget.after(0, lambda: callback(img))
        except Exception as exc:
            log.debug("Photo fetch failed (%s): %s", url, exc)

    threading.Thread(target=_worker, daemon=True).start()


def resolve_photo_url(url: str, base: str) -> str:
    """Prefix relative photo paths with *base* URL."""
    if url and url.startswith("/"):
        return base + url
    return url or ""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN STUDENT CARD — the large card shown at the top of the screen
# ═══════════════════════════════════════════════════════════════════════════════


class MainStudentCard(ctk.CTkFrame):
    """
    Displays full details of the **currently scanned** student:
    photo, name, seat number, college, department, access status, and time.

    Layout (RTL)::

        ┌──────────────────────────────┐
        │  [Info left]   [Photo right] │
        └──────────────────────────────┘
    """

    def __init__(self, master: Any, **kwargs: Any) -> None:
        super().__init__(
            master,
            fg_color=Colors.CARD_BG,
            corner_radius=20,
            border_width=3,
            border_color=Colors.CARD_BORDER,
            **kwargs,
        )

        self._photo_ref: Optional[ImageTk.PhotoImage] = None
        self._status: str = "denied"
        self.is_empty: bool = True

        # ── layout ───────────────────────────────────────────────────
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(expand=True, fill="both", padx=40, pady=30)

        # Right — photo
        photo_frame = ctk.CTkFrame(
            container, fg_color=Colors.PHOTO_BG, corner_radius=15,
        )
        photo_frame.pack(side="right", padx=20)
        self.photo_label = ctk.CTkLabel(
            photo_frame, text="",
            width=MAIN_PHOTO_SIZE[0], height=MAIN_PHOTO_SIZE[1],
        )
        self.photo_label.pack(padx=20, pady=20)

        # Left — text info
        info = ctk.CTkFrame(container, fg_color="transparent")
        info.pack(side="left", expand=True, fill="both", padx=30)

        # Status badge
        self.status_frame = ctk.CTkFrame(
            info, fg_color=Colors.GREEN, corner_radius=10, height=60,
        )
        self.status_frame.pack(fill="x", pady=(0, 20))
        self.status_frame.pack_propagate(False)
        self.status_label = ctk.CTkLabel(
            self.status_frame, text="",
            font=ctk.CTkFont(family="Amiri", size=36, weight="bold"),
            text_color="white",
        )
        self.status_label.pack(expand=True)

        # Student name
        self.name_label = ctk.CTkLabel(
            info, text="",
            font=ctk.CTkFont(family="Amiri", size=56, weight="bold"),
            text_color="white", anchor="e", justify="right",
        )
        self.name_label.pack(anchor="e", pady=(10, 15))

        # Seat number
        self.seat_label = ctk.CTkLabel(
            info, text="",
            font=ctk.CTkFont(family="Amiri", size=38, weight="bold"),
            text_color=Colors.BLUE_TEXT, anchor="e",
        )
        self.seat_label.pack(anchor="e", pady=8)

        # College
        self.college_label = ctk.CTkLabel(
            info, text="",
            font=ctk.CTkFont(family="Amiri", size=32),
            text_color=Colors.MUTED_TEXT, anchor="e", justify="right",
        )
        self.college_label.pack(anchor="e", pady=5)

        # Department
        self.dept_label = ctk.CTkLabel(
            info, text="",
            font=ctk.CTkFont(family="Amiri", size=28),
            text_color=Colors.MUTED_TEXT, anchor="e",
        )
        self.dept_label.pack(anchor="e", pady=5)

        # Scan timestamp
        time_frame = ctk.CTkFrame(info, fg_color=Colors.TIME_BG, corner_radius=10)
        time_frame.pack(anchor="e", pady=(30, 0))
        self.time_label = ctk.CTkLabel(
            time_frame, text="",
            font=ctk.CTkFont(size=24, weight="bold"),
            text_color=Colors.MUTED_TEXT,
        )
        self.time_label.pack(padx=20, pady=10)

    # ── public API ────────────────────────────────────────────────────────────

    def set_student(
        self, data: dict[str, Any], status: str = "granted",
    ) -> None:
        """
        Populate the card with student information.

        Parameters
        ----------
        data :
            Keys: ``name``, ``seat_number``, ``college``, ``department``, ``photo``.
        status :
            ``"granted"`` or ``"denied"``.
        """
        self.is_empty = False
        self._status = status

        # Status badge
        if status == "granted":
            self.status_frame.configure(fg_color=Colors.GREEN)
            self.status_label.configure(text=reshape_arabic("✓ الدخول مسموح"))
        else:
            self.status_frame.configure(fg_color=Colors.RED)
            self.status_label.configure(text=reshape_arabic("✗ الدخول مرفوض"))

        # Text fields
        self.name_label.configure(text=reshape_arabic(data.get("name", "")))

        seat = data.get("seat_number", "")
        self.seat_label.configure(
            text=reshape_arabic(f"رقم الجلوس: {seat}") if seat else "",
        )

        college = data.get("college", "")
        college_ar = COLLEGE_NAMES.get(college, college)
        self.college_label.configure(
            text=reshape_arabic(college_ar) if college_ar else "",
        )

        dept = data.get("department", "")
        dept_ar = DEPARTMENT_NAMES.get(dept, dept)
        self.dept_label.configure(
            text=reshape_arabic(dept_ar) if dept_ar else "",
        )

        self.time_label.configure(
            text=f"⏰ {datetime.now().strftime('%I:%M:%S %p')}",
        )

    def set_photo(self, pil_img: Image.Image) -> None:
        """Display a downloaded photo (must be called on the main thread)."""
        try:
            self._photo_ref = ImageTk.PhotoImage(pil_img)
            self.photo_label.configure(image=self._photo_ref)
        except Exception as exc:
            log.debug("Main card photo display error: %s", exc)

    def clear(self) -> None:
        """Reset this card to an empty state."""
        self.is_empty = True
        self._status = "denied"
        self._photo_ref = None
        self.photo_label.configure(image="")
        for lbl in (
            self.status_label, self.name_label, self.seat_label,
            self.college_label, self.dept_label, self.time_label,
        ):
            lbl.configure(text="")


# ═══════════════════════════════════════════════════════════════════════════════
# SMALL STUDENT CARD — history row at the bottom (previous entries)
# ═══════════════════════════════════════════════════════════════════════════════


class SmallStudentCard(ctk.CTkFrame):
    """
    A compact card showing a previously scanned student.
    Used in the "recent entries" row at the bottom of the display.

    Layout::

        ┌──────────┐
        │  [Photo]  │
        │  ──────── │  ← status colour bar
        │   Name    │
        │   Seat    │
        │   Time    │
        └──────────┘
    """

    def __init__(self, master: Any, **kwargs: Any) -> None:
        super().__init__(
            master,
            fg_color=Colors.CARD_BG,
            corner_radius=15,
            border_width=2,
            border_color=Colors.HISTORY_BORDER,
            **kwargs,
        )

        self._photo_ref: Optional[ImageTk.PhotoImage] = None
        self.is_empty: bool = True

        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(expand=True, fill="both", padx=20, pady=20)

        # Photo
        photo_frame = ctk.CTkFrame(
            container, fg_color=Colors.PHOTO_BG, corner_radius=10,
        )
        photo_frame.pack(pady=(0, 12))
        self.photo_label = ctk.CTkLabel(
            photo_frame, text="",
            width=SMALL_PHOTO_SIZE[0], height=SMALL_PHOTO_SIZE[1],
        )
        self.photo_label.pack(padx=5, pady=5)

        # Status colour bar
        self.status_dot = ctk.CTkFrame(
            container, height=6, fg_color=Colors.DOT_IDLE, corner_radius=3,
        )
        self.status_dot.pack(fill="x", pady=(0, 8))

        # Name
        self.name_label = ctk.CTkLabel(
            container, text="",
            font=ctk.CTkFont(family="Amiri", size=24, weight="bold"),
            text_color="white", wraplength=280, justify="center",
        )
        self.name_label.pack(pady=3)

        # Seat
        self.seat_label = ctk.CTkLabel(
            container, text="",
            font=ctk.CTkFont(family="Amiri", size=18),
            text_color=Colors.BLUE_TEXT,
        )
        self.seat_label.pack(pady=2)

        # Time
        self.time_label = ctk.CTkLabel(
            container, text="",
            font=ctk.CTkFont(size=14),
            text_color=Colors.DOT_IDLE,
        )
        self.time_label.pack(pady=(5, 0))

    # ── public API ────────────────────────────────────────────────────────────

    def load_from(self, entry: dict[str, Any]) -> None:
        """
        Populate from a history-entry dict (not from widget text).

        Parameters
        ----------
        entry :
            Keys: ``name``, ``seat_number``, ``photo_url``, ``status``, ``time``.
        """
        self.is_empty = False
        status = entry.get("status", "denied")

        self.status_dot.configure(
            fg_color=Colors.GREEN if status == "granted" else Colors.RED,
        )
        self.name_label.configure(text=reshape_arabic(entry.get("name", "")))

        seat = entry.get("seat_number", "")
        self.seat_label.configure(
            text=reshape_arabic(f"رقم: {seat}") if seat else "",
        )
        self.time_label.configure(text=entry.get("time", ""))

        photo_url = entry.get("photo_url", "")
        if photo_url:
            load_photo_async(
                photo_url, SMALL_PHOTO_SIZE, self, self._show_photo,
            )

    def clear(self) -> None:
        """Reset to empty state."""
        self.is_empty = True
        self._photo_ref = None
        self.photo_label.configure(image="")
        self.status_dot.configure(fg_color=Colors.DOT_CLEAR)
        for lbl in (self.name_label, self.seat_label, self.time_label):
            lbl.configure(text="")

    # ── private ───────────────────────────────────────────────────────────────

    def _show_photo(self, pil_img: Image.Image) -> None:
        """Callback — runs on the main thread after photo download."""
        try:
            self._photo_ref = ImageTk.PhotoImage(pil_img)
            self.photo_label.configure(image=self._photo_ref)
        except Exception as exc:
            log.debug("Small card photo display error: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════


class GateApp(ctk.CTk):
    """
    Fullscreen kiosk application window.

    Display layout::

        ┌──────────────────────────────────────────────┐
        │  🏛️  نظام مراقبة بوابة الدخول  ● Arduino  HH:MM │  ← header
        ├──────────────────────────────────────────────┤
        │                                              │
        │          [ Main Student Card ]               │  ← current scan
        │                                              │
        ├──────────────────────────────────────────────┤
        │  آخر الحضور                                  │
        │  [Card 1]      [Card 2]      [Card 3]       │  ← history
        ├──────────────────────────────────────────────┤
        │  ✓ النظام جاهز - في انتظار مسح البطاقة...   │  ← status bar
        └──────────────────────────────────────────────┘

    Internal State
    --------------
    - ``_history``  : list of plain dicts (newest-first).
    - ``_current``  : dict of the student on the main card.
    - ``_debounce`` : ``{barcode: monotonic_time}`` for rapid-scan rejection.
    """

    MAX_HISTORY: int = 3

    def __init__(
        self,
        config: GateConfig,
        controller: GateController,
    ) -> None:
        super().__init__()
        self.title("نظام مراقبة البوابة - Gate Access System")
        self.attributes("-fullscreen", True)
        self.configure(fg_color=Colors.BG_DARK)

        self._config: GateConfig = config
        self._controller: GateController = controller
        self._offline_cache: OfflineCache = OfflineCache(
            ttl=config.offline_cache_ttl, enabled=config.offline_mode,
        )

        # ── state ─────────────────────────────────────────────────────────────
        self._history: list[dict[str, Any]] = []
        self._current: Optional[dict[str, Any]] = None
        self._debounce: dict[str, float] = {}
        self._debounce_lock: threading.Lock = threading.Lock()

        # ── UI ────────────────────────────────────────────────────────────────
        self._build_header()
        self._build_content()
        self._build_status_bar()

        # ── periodic updates ──────────────────────────────────────────────────
        self._update_arduino_indicator()

        # ── start scanner in the background ───────────────────────────────────
        threading.Thread(
            target=self._scanner_loop, daemon=True, name="scanner",
        ).start()
        log.info("GUI started — waiting for scans")

    # ═════════════════════════════════════════════════════════════════════════
    # UI CONSTRUCTION
    # ═════════════════════════════════════════════════════════════════════════

    def _build_header(self) -> None:
        """University branding header with logo, title, Arduino indicator, and clock."""
        header = ctk.CTkFrame(
            self, height=100, fg_color=Colors.HEADER_BG, corner_radius=0,
        )
        header.pack(fill="x")
        header.pack_propagate(False)

        hc = ctk.CTkFrame(header, fg_color="transparent")
        hc.pack(expand=True, fill="both", padx=40)

        # Logo (left)
        ctk.CTkLabel(
            hc, text="🏛️", font=ctk.CTkFont(size=48),
        ).pack(side="left", padx=20)

        # Title (centre)
        tf = ctk.CTkFrame(hc, fg_color="transparent")
        tf.pack(expand=True)
        ctk.CTkLabel(
            tf,
            text=reshape_arabic("نظام مراقبة بوابة الدخول"),
            font=ctk.CTkFont(family="Amiri", size=48, weight="bold"),
            text_color="white",
        ).pack()
        ctk.CTkLabel(
            tf,
            text="Gate Access Monitoring System",
            font=ctk.CTkFont(size=20),
            text_color=Colors.LIGHT_BLUE,
        ).pack()

        # Clock (right)
        self._clock_label = ctk.CTkLabel(
            hc, text="",
            font=ctk.CTkFont(size=24, weight="bold"),
            text_color=Colors.LIGHT_BLUE,
        )
        self._clock_label.pack(side="right", padx=20)
        self._tick_clock()

        # Arduino connection indicator (right, before clock)
        self._arduino_dot = ctk.CTkLabel(
            hc, text="●",
            font=ctk.CTkFont(size=16),
            text_color=Colors.RED,
        )
        self._arduino_dot.pack(side="right", padx=(0, 4))
        self._arduino_label = ctk.CTkLabel(
            hc, text="Arduino",
            font=ctk.CTkFont(size=14),
            text_color=Colors.MUTED_TEXT,
        )
        self._arduino_label.pack(side="right", padx=(0, 10))

    def _build_content(self) -> None:
        """Main content area: big card + history row."""
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(expand=True, fill="both", padx=30, pady=20)

        # Main card
        top = ctk.CTkFrame(content, fg_color="transparent")
        top.pack(fill="both", expand=True, pady=(0, 20))
        self.main_card = MainStudentCard(top)
        self.main_card.pack(fill="both", expand=True)

        # History label
        ctk.CTkLabel(
            content,
            text=reshape_arabic("آخر الحضور"),
            font=ctk.CTkFont(family="Amiri", size=32, weight="bold"),
            text_color=Colors.MUTED_TEXT, anchor="e",
        ).pack(anchor="e", pady=(0, 10), padx=20)

        # History cards row
        bottom = ctk.CTkFrame(content, fg_color="transparent", height=500)
        bottom.pack(fill="x", pady=(0, 10))
        bottom.pack_propagate(False)

        self.small_cards: list[SmallStudentCard] = []
        for _ in range(self.MAX_HISTORY):
            card = SmallStudentCard(bottom)
            card.pack(side="right", expand=True, fill="both", padx=10)
            self.small_cards.append(card)

    def _build_status_bar(self) -> None:
        """Bottom status bar showing system / scan status."""
        bar = ctk.CTkFrame(
            self, height=70, fg_color=Colors.STATUS_BAR_BG, corner_radius=0,
        )
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._status_label = ctk.CTkLabel(
            bar,
            text=reshape_arabic("✓ النظام جاهز - في انتظار مسح البطاقة..."),
            font=ctk.CTkFont(family="Amiri", size=32, weight="bold"),
            text_color=Colors.GREEN,
        )
        self._status_label.pack(expand=True)

    # ═════════════════════════════════════════════════════════════════════════
    # PERIODIC UI UPDATES
    # ═════════════════════════════════════════════════════════════════════════

    def _tick_clock(self) -> None:
        """Update the header clock every second."""
        self._clock_label.configure(text=datetime.now().strftime("%I:%M:%S %p"))
        self.after(CLOCK_UPDATE_MS, self._tick_clock)

    def _update_arduino_indicator(self) -> None:
        """Refresh the Arduino connection dot in the header."""
        if self._controller.connected:
            self._arduino_dot.configure(text_color=Colors.GREEN)
        else:
            self._arduino_dot.configure(text_color=Colors.RED)
        self.after(INDICATOR_UPDATE_MS, self._update_arduino_indicator)

    # ═════════════════════════════════════════════════════════════════════════
    # ENTRY MANAGEMENT — all UI updates run on the Tk main thread
    # ═════════════════════════════════════════════════════════════════════════

    def _push_entry(self, student: dict[str, Any], status: str) -> None:
        """
        Show a new scan result on the display.

        **Must be called on the main Tkinter thread** (via ``self.after``).

        1. Build a lightweight history dict from the student data.
        2. Demote the current main-card entry to the history list.
        3. Refresh all small cards from ``self._history`` (newest first).
        4. Update the main card with the new scan.
        5. Update the status bar.
        """
        entry: dict[str, Any] = {
            "name":        student.get("name", ""),
            "seat_number": student.get("seat_number", ""),
            "photo_url":   resolve_photo_url(
                student.get("photo", ""), self._config.base_media_url,
            ),
            "status":      status,
            "time":        datetime.now().strftime("%I:%M %p"),
        }

        # Demote current → history
        if self._current is not None:
            self._history.insert(0, self._current)
            self._history = self._history[: self.MAX_HISTORY]
        self._current = entry

        # Refresh history cards
        for i, card in enumerate(self.small_cards):
            if i < len(self._history):
                card.load_from(self._history[i])
            else:
                card.clear()

        # Main card
        self.main_card.set_student(student, status)

        # Load photo asynchronously
        photo_url = resolve_photo_url(
            student.get("photo", ""), self._config.base_media_url,
        )
        if photo_url:
            load_photo_async(
                photo_url, MAIN_PHOTO_SIZE, self, self.main_card.set_photo,
            )

        # Status bar
        if status == "granted":
            name = student.get("name", "")
            self._status_label.configure(
                text=f"{reshape_arabic('مرحباً')} {reshape_arabic(name)} ✓",
                text_color=Colors.GREEN,
            )
        else:
            self._status_label.configure(
                text=reshape_arabic("✗ تم رفض الدخول - بطاقة غير صالحة"),
                text_color=Colors.RED,
            )

        # Auto-reset status bar
        self.after(STATUS_BAR_RESET_MS, self._reset_status_bar)

    def _reset_status_bar(self) -> None:
        """Restore the idle status bar message."""
        self._status_label.configure(
            text=reshape_arabic("✓ النظام جاهز - في انتظار مسح البطاقة..."),
            text_color=Colors.GREEN,
        )

    # ═════════════════════════════════════════════════════════════════════════
    # SCAN PROCESSING — runs in a background thread
    # ═════════════════════════════════════════════════════════════════════════

    def _process_scan(self, code: str) -> None:
        """
        Call the access-check API with retry logic, then schedule the UI update.

        Runs in a **daemon thread** — all Tk updates are posted via
        ``self.after(0, …)`` to remain thread-safe.

        Retry policy:
        - Up to ``api_max_retries`` attempts.
        - Exponential backoff starting at ``api_retry_delay`` seconds.
        - Only retryable errors (timeout, 5xx, ConnectionError) are retried.
        - 4xx and SSL errors cause immediate failure.
        """
        _health_state["total_scans"] += 1
        _health_state["last_scan_time"] = time.monotonic()

        headers: dict[str, str] = {
            "Accept":       "application/json",
            "Content-Type": "application/json",
        }
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        ssl_verify = self._config.ssl_verify
        max_retries = self._config.api_max_retries
        base_delay = self._config.api_retry_delay
        api_reachable = False

        for attempt in range(1, max_retries + 1):
            try:
                log.info("Scanning barcode: %s (attempt %d/%d)",
                         code, attempt, max_retries)

                r = requests.post(
                    self._config.api_url,
                    json={"bar_code": code},
                    headers=headers,
                    timeout=self._config.api_timeout,
                    verify=ssl_verify,
                )

                # ── Non-retryable HTTP errors ─────────────────────────────────
                if r.status_code in (400, 401, 403, 404):
                    log.warning("API HTTP %d (non-retryable) for barcode %s",
                                r.status_code, code)
                    break

                # ── Successful response ───────────────────────────────────────
                if r.status_code == 200:
                    api_reachable = True
                    _health_state["last_api_success_time"] = time.monotonic()

                    try:
                        data = r.json()
                        allowed, student = validate_api_response(data)
                    except (ValidationError, ValueError) as exc:
                        log.error("Malformed API response for barcode %s: %s",
                                  code, exc)
                        break

                    if allowed and student:
                        log.info("ACCESS GRANTED — %s (barcode %s)",
                                 student.get("name", "?"), code)
                        _health_state["total_granted"] += 1
                        self._offline_cache.store(code, student)
                        self.after(
                            0, lambda s=student: self._push_entry(s, "granted"),
                        )
                        self._controller.grant_access_sequence()
                        return

                    # Explicit denial from API — do not retry
                    log.info("ACCESS DENIED by API — barcode %s", code)
                    break

                # ── 5xx — retryable ───────────────────────────────────────────
                log.warning("API HTTP %d for barcode %s (attempt %d/%d)",
                            r.status_code, code, attempt, max_retries)

            except requests.exceptions.SSLError as exc:
                log.error("SSL error for barcode %s: %s", code, exc)
                break  # SSL errors are never retried

            except requests.exceptions.ConnectionError as exc:
                log.warning("Network unreachable for barcode %s (attempt %d/%d): %s",
                            code, attempt, max_retries, exc)

            except requests.exceptions.Timeout:
                log.warning("API timeout for barcode %s (attempt %d/%d)",
                            code, attempt, max_retries)

            except Exception as exc:
                log.error("Unexpected API error for barcode %s: %s", code, exc)
                break

            # ── Wait before next retry (exponential backoff) ──────────────────
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                log.debug("Retrying in %.1fs…", delay)
                time.sleep(delay)

        # ── All retries exhausted / non-retryable error ───────────────────────

        # Try offline cache if the API was not reachable at all
        if not api_reachable:
            cached = self._offline_cache.lookup(code)
            if cached:
                barcode_hash = hashlib.sha256(code.encode()).hexdigest()[:12]
                log.info("OFFLINE CACHE HIT (hash %s) — granting access",
                         barcode_hash)
                _health_state["total_granted"] += 1
                self.after(
                    0, lambda s=cached: self._push_entry(s, "granted"),
                )
                self._controller.grant_access_sequence()
                return

        # ── Denied — generic message (never expose raw barcode on screen) ─────
        _health_state["total_denied"] += 1
        denied_data: dict[str, Any] = {
            "name": "بطاقة غير صالحة",
            "seat_number": "",
            "college": "",
            "department": "",
        }
        self.after(0, lambda d=denied_data: self._push_entry(d, "denied"))
        self._controller.deny_access_sequence()

    # ═════════════════════════════════════════════════════════════════════════
    # DEBOUNCE — thread-safe
    # ═════════════════════════════════════════════════════════════════════════

    def _handle_code(self, code: str) -> None:
        """
        Accept a scanned barcode, validate it, apply debounce, then
        dispatch to ``_process_scan`` in a background thread.
        """
        # ── input validation ──────────────────────────────────────────────────
        if not validate_barcode(code):
            return

        # ── debounce (thread-safe) ────────────────────────────────────────────
        now = time.monotonic()
        with self._debounce_lock:
            last = self._debounce.get(code, 0.0)
            if now - last < self._config.debounce_seconds:
                log.debug("Debounce: ignored duplicate %s (%.1fs ago)",
                          code, now - last)
                return
            self._debounce[code] = now

            # Prune stale entries so the dict stays small
            cutoff = now - self._config.debounce_seconds * 20
            self._debounce = {
                k: v for k, v in self._debounce.items() if v > cutoff
            }

        threading.Thread(
            target=self._process_scan, args=(code,), daemon=True,
        ).start()

    # ═════════════════════════════════════════════════════════════════════════
    # SCANNER INPUT LOOP
    # ═════════════════════════════════════════════════════════════════════════

    def _scanner_loop(self) -> None:
        """Route barcode input to serial or stdin based on config."""
        if self._config.barcode_serial_port:
            self._serial_scanner()
        else:
            self._stdin_scanner()

    def _stdin_scanner(self) -> None:
        """
        Read barcodes from **stdin** (USB HID keyboard mode).

        Each scan produces a line of text terminated by Enter.
        """
        log.info("Barcode input: stdin (USB HID keyboard mode)")
        while True:
            try:
                code = input().strip()
                if code:
                    self._handle_code(code)
            except EOFError:
                log.warning("stdin closed — barcode scanning stopped")
                break
            except Exception as exc:
                log.error("stdin scanner error: %s", exc)
                time.sleep(1)

    def _serial_scanner(self) -> None:
        """
        Read barcodes from a **dedicated serial port** with auto-reconnect.
        """
        port = self._config.barcode_serial_port
        baud = self._config.barcode_baud_rate
        log.info("Barcode input: serial %s @ %d baud", port, baud)
        while True:
            try:
                with serial.Serial(port, baud, timeout=1) as sc:
                    log.info("Barcode scanner connected on %s", port)
                    while True:
                        line = sc.readline().decode(errors="ignore").strip()
                        if line:
                            self._handle_code(line)
            except serial.SerialException as exc:
                log.error("Barcode serial error: %s — retrying in 5 s", exc)
                time.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("══════════════════════════════════════════════════")
    log.info(" Gate Access Monitoring System — starting up")
    log.info("══════════════════════════════════════════════════")

    _health_state["start_time"] = time.monotonic()

    # ── Load and validate configuration ───────────────────────────────────────
    try:
        cfg = GateConfig.from_env()
    except GateError as e:
        log.critical("Configuration error: %s", e)
        sys.exit(1)

    log.info("API URL      : %s", cfg.api_url)
    log.info("API key      : %s", "configured" if cfg.api_key else "NOT SET")
    log.info("SSL verify   : %s", cfg.ssl_verify)
    log.info("Serial port  : %s @ %d baud", cfg.serial_port, cfg.baud_rate)
    log.info("Gate open    : %.1fs", cfg.gate_open_duration)
    log.info("Debounce     : %.1fs", cfg.debounce_seconds)
    log.info("Offline mode : %s (TTL %ds)", cfg.offline_mode, int(cfg.offline_cache_ttl))
    log.info("Health port  : %s", cfg.health_port or "disabled")

    # ── Initialise gate controller ────────────────────────────────────────────
    gate_ctrl = GateController(cfg)
    gate_ctrl.start()

    # ── Start health-check server (if configured) ─────────────────────────────
    health_srv: Optional[HealthCheckServer] = None
    if cfg.health_port > 0:
        health_srv = HealthCheckServer(cfg.health_port, gate_ctrl)
        health_srv.start()

    # ── Launch the GUI ────────────────────────────────────────────────────────
    app = GateApp(cfg, gate_ctrl)
    try:
        app.mainloop()
    finally:
        log.info("Shutting down…")
        gate_ctrl.shutdown()
        if health_srv:
            health_srv.stop()
        log.info("══════════════════════════════════════════════════")
        log.info(" Gate Access Monitoring System — stopped")
        log.info("══════════════════════════════════════════════════")
