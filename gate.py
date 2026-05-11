#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# cspell:disable
"""
Gate Access Monitoring System — BATU University
================================================

Fullscreen kiosk app for the Raspberry Pi 4B. Reads student RFID cards
via an RC522 reader, checks them against a REST API, and opens the gate
if access is granted. Everything else — photos, names, history — gets
displayed on an HDMI monitor.

Hardware
--------
- **Raspberry Pi 4B** — runs this script, drives the display.
- **RC522 RFID Module** — connected directly to the RPi via SPI.
- **Arduino Mega** — relay between RPi and solenoid lock. Listens for
  ``OPEN`` and ``CLOSE`` commands over USB serial at 9600 baud.
- **Solenoid Lock** — the only physical gate component Arduino controls.
- **Monitor** — HDMI display for the CustomTkinter GUI.

Key environment variables (see ``config.env.example`` for the full list)
------------------------------------------------------------------------
``GATE_API_URL``            API endpoint (defaults to the production server).
``GATE_API_KEY``            Bearer token in the ``Authorization`` header.
``GATE_API_UID_FIELD``      JSON field name for the UID (default ``bar_code``).
``GATE_VERIFY_SSL``         Set ``false`` to disable SSL verification.
``GATE_API_TIMEOUT``        Request timeout in seconds (default ``3``).
``GATE_SERIAL_PORT``        Arduino serial port (default ``/dev/ttyACM0``).
``GATE_BAUD_RATE``          Must match the Arduino sketch (default ``9600``).
``RFID_READER_TYPE``        ``RC522``, ``PN532``, or ``SIMULATION``.
``CARD_DEBOUNCE_SECONDS``   Ignore the same card within N seconds (default ``3``).
``UID_HASH_KEY``            32-byte hex HMAC key — replace this on the RPi.
``SIMULATION_MODE``         Generate fake card reads without hardware.
``BASE_MEDIA_URL``          Photo URL prefix (defaults to the production server).
``GATE_OFFLINE_MODE``       Fall back to local cache when the API is down.
``GATE_OFFLINE_CACHE_TTL``  Offline cache TTL seconds (default ``300``).
``GATE_HEALTH_PORT``        HTTP health check port (default ``0`` = disabled).

Author : Khalil Muhammad
License: MIT
"""


# ════════════════════
# IMPORTS
# ════════════════════

from __future__ import annotations

import abc
import collections
import enum
import hashlib
import hmac
import http.server
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import queue
import random
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Any, Callable, Optional

import arabic_reshaper
import customtkinter as ctk
import requests
from requests.adapters import HTTPAdapter
import serial
from bidi.algorithm import get_display
from PIL import Image, ImageTk

# RFID hardware — optional (graceful fallback for dev / simulation)
try:
    from mfrc522 import MFRC522
    _RFID_HW_AVAILABLE = True
except ImportError:
    _RFID_HW_AVAILABLE = False

# ════════════════════
# CUSTOM EXCEPTIONS
# ════════════════════


class GateError(Exception):
    """Base exception for all gate-system errors."""


class ArduinoError(GateError):
    """Raised when the Arduino serial connection fails."""


class APIError(GateError):
    """Raised when the access-check API returns an invalid response."""


class ValidationError(GateError):
    """Raised when input validation fails (UID, API response, etc.)."""


class RFIDError(GateError):
    """Raised when the RFID reader encounters a hardware or protocol error."""


# ════════════════════
# LOGGING — file + console, with log rotation
# ════════════════════

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

# ════════════════════════════════════════════════════════════
# MODULE-LEVEL CONSTANTS (non-configurable timing / sizing)
# ════════════════════════════════════════════════════════════

ARDUINO_CONNECT_DELAY: float = 2.0      # seconds after opening serial
GATE_CLOSE_DELAY: float = 0.3           # brief pause after CLOSE
STATUS_BAR_RESET_MS: int = 4000         # ms before status bar resets
CLOCK_UPDATE_MS: int = 1000             # ms between clock ticks
INDICATOR_UPDATE_MS: int = 2000         # ms between Arduino indicator checks
ARDUINO_RECONNECT_MIN: float = 2.0      # starting reconnect delay
ARDUINO_RECONNECT_MAX: float = 60.0     # max reconnect delay (cap)
GATE_OPEN_CONFIRM_TIMEOUT: float = 3.0  # seconds to wait for OPEN ack
PHOTO_FETCH_TIMEOUT: float = 8.0        # seconds for photo downloads

# RFID / card-detection timing defaults
RFID_POLL_INTERVAL: float = 0.05        # seconds between card polls
UID_ALL_ZEROS: bytes = b"\x00\x00\x00\x00"  # reject test cards
ROLLING_LATENCY_WINDOW: int = 100        # samples for rolling avg latency

# Photo widget dimensions (pixels)
MAIN_PHOTO_SIZE: tuple[int, int] = (400, 300)
SMALL_PHOTO_SIZE: tuple[int, int] = (240, 300)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION — typed dataclass loaded from environment variables
# ══════════════════════════════════════════════════════════════════════


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
    api_uid_field: str

    # ── Arduino Mega / gate controller ────────────────────────────────────────
    serial_port: str
    baud_rate: int

    # ── RFID reader ───────────────────────────────────────────────────────────
    rfid_reader_type: str
    rc522_rst_pin: int
    rc522_spi_bus: int
    rc522_spi_device: int

    # ── MIFARE ────────────────────────────────────────────────────────────────
    mifare_default_key: str
    uid_hash_key: str

    # ── Gate timing ───────────────────────────────────────────────────────────
    gate_open_duration: float
    card_debounce_seconds: float

    # ── Performance ───────────────────────────────────────────────────────────
    api_pool_size: int

    # ── Media ─────────────────────────────────────────────────────────────────
    base_media_url: str

    # ── Offline cache ─────────────────────────────────────────────────────────
    offline_mode: bool
    offline_cache_ttl: float

    # ── Health check ──────────────────────────────────────────────────────────
    health_port: int

    # ── Simulation ────────────────────────────────────────────────────────────
    simulation_mode: bool
    simulation_interval: float
    simulation_success_rate: float

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
        api_url = os.getenv(
            "GATE_API_URL",
            "https://batu-gate.alnzam.online/api/v1/gate/check-access",
        ).strip()

        api_cert_path = os.getenv("GATE_API_CERT_PATH", "").strip()
        if api_cert_path and not os.path.isfile(api_cert_path):
            raise GateError(
                f"GATE_API_CERT_PATH points to a non-existent file: {api_cert_path}"
            )

        gate_open_duration = float(os.getenv("GATE_OPEN_DURATION", "5"))
        if gate_open_duration <= 0:
            raise GateError("GATE_OPEN_DURATION must be a positive number.")

        card_debounce = float(os.getenv("CARD_DEBOUNCE_SECONDS", "0.5"))
        if card_debounce < 0:
            raise GateError("CARD_DEBOUNCE_SECONDS must be non-negative.")

        sim_rate = float(os.getenv("SIMULATION_SUCCESS_RATE", "0.8"))
        if not 0.0 <= sim_rate <= 1.0:
            raise GateError("SIMULATION_SUCCESS_RATE must be between 0.0 and 1.0.")

        return cls(
            api_url=api_url,
            api_key=os.getenv("GATE_API_KEY", "").strip(),
            api_cert_path=api_cert_path,
            verify_ssl=_bool_env("GATE_VERIFY_SSL", True),
            api_timeout=float(os.getenv("GATE_API_TIMEOUT", "3.0")),
            api_max_retries=int(os.getenv("GATE_API_MAX_RETRIES", "3")),
            api_retry_delay=float(os.getenv("GATE_API_RETRY_DELAY", "1")),
            api_uid_field=os.getenv("GATE_API_UID_FIELD", "bar_code").strip(),
            serial_port=os.getenv("GATE_SERIAL_PORT", "/dev/ttyACM0"),
            baud_rate=int(os.getenv("GATE_BAUD_RATE", "9600")),
            rfid_reader_type=os.getenv("RFID_READER_TYPE", "RC522").strip().upper(),
            rc522_rst_pin=int(os.getenv("RC522_RST_PIN", "25")),
            rc522_spi_bus=int(os.getenv("RC522_SPI_BUS", "0")),
            rc522_spi_device=int(os.getenv("RC522_SPI_DEVICE", "0")),
            mifare_default_key=os.getenv("MIFARE_DEFAULT_KEY", "FFFFFFFFFFFF"),
            uid_hash_key=os.getenv("UID_HASH_KEY", "change-me-generate-with-openssl"),
            gate_open_duration=gate_open_duration,
            card_debounce_seconds=card_debounce,
            api_pool_size=int(os.getenv("GATE_API_POOL_SIZE", "20")),
            base_media_url=os.getenv("BASE_MEDIA_URL", "https://batu-gate.alnzam.online").strip(),
            offline_mode=_bool_env("GATE_OFFLINE_MODE", False),
            offline_cache_ttl=float(os.getenv("GATE_OFFLINE_CACHE_TTL", "300")),
            health_port=int(os.getenv("GATE_HEALTH_PORT", "0")),
            simulation_mode=_bool_env("SIMULATION_MODE", False),
            simulation_interval=float(os.getenv("SIMULATION_INTERVAL_SECONDS", "3")),
            simulation_success_rate=sim_rate,
        )


# ════════════════════
# APPEARANCE
# ════════════════════

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


# ═════════════════════════════════════════════════════════
# TRANSLATION TABLES — English slug → Arabic display name
# ═════════════════════════════════════════════════════════

# cspell:disable
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
# cspell:enable

# ═══════════════════════════
# GATE STATUS ENUM
# ═══════════════════════════


class GateStatus(enum.Enum):
    """Physical state of the turnstile gate."""

    UNKNOWN = "unknown"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"


# ════════════════════
# CARD TYPE ENUM
# ════════════════════


class CardType(enum.Enum):
    """MIFARE card types detected by the RFID reader."""

    UNKNOWN = "unknown"
    MIFARE_1K = "mifare_1k"
    MIFARE_4K = "mifare_4k"
    MIFARE_ULTRALIGHT = "mifare_ultralight"
    NTAG = "ntag"


# ════════════════════
# RFID CARD DATACLASS
# ════════════════════


@dataclass(frozen=True)
class RFIDCard:
    """Immutable representation of a scanned RFID/NFC card.

    Attributes
    ----------
    uid : str
        Normalised UID string, e.g. ``"A3:B7:C2:D4"``.
    uid_bytes : bytes
        Raw UID bytes from the reader.
    card_type : CardType
        Detected MIFARE card type (from SAK byte).
    atqa : bytes
        Answer To Request Type A.
    sak : bytes
        Select Acknowledge byte(s).
    read_timestamp : float
        ``time.monotonic()`` when the card was read.
    """

    uid: str
    uid_bytes: bytes
    card_type: CardType
    atqa: bytes
    sak: bytes
    read_timestamp: float


# ════════════════════════════════════════════════════════════
# UID VALIDATOR — normalisation, validation, hashing
# ════════════════════════════════════════════════════════════


class UIDValidator:
    """Validates, normalises, and hashes MIFARE card UIDs.

    All methods are pure static helpers — no instance state.
    """

    VALID_UID_LENGTHS: tuple[int, ...] = (4, 7, 10)

    @staticmethod
    def validate(uid_bytes: bytes) -> bool:
        """Check that *uid_bytes* is a valid MIFARE UID.

        Rules
        -----
        - Length must be 4, 7, or 10 bytes.
        - All-zero UIDs (test/blank cards) are rejected.
        """
        if len(uid_bytes) not in UIDValidator.VALID_UID_LENGTHS:
            log.warning(
                "SECURITY: UID length %d invalid (expected %s)",
                len(uid_bytes), UIDValidator.VALID_UID_LENGTHS,
            )
            return False
        if uid_bytes == b"\x00" * len(uid_bytes):
            log.warning("SECURITY: all-zero UID rejected (test/blank card)")
            return False
        return True

    @staticmethod
    def normalize(uid_bytes: bytes) -> str:
        """Convert raw UID bytes to uppercase colon-separated hex.

        Example: ``b'\\xa3\\xb7\\xc2\\xd4'`` → ``'A3:B7:C2:D4'``
        """
        return ":".join(f"{b:02X}" for b in uid_bytes)

    @staticmethod
    def to_api_format(uid: str) -> str:
        """Remove separators for hex transmission.

        Example: ``'A3:B7:C2:D4'`` → ``'A3B7C2D4'``

        .. note::
            The API expects a **decimal** value — use :meth:`to_decimal`
            for the actual API payload.
        """
        return uid.replace(":", "").upper()

    @staticmethod
    def to_decimal(uid_bytes: bytes) -> str:
        """Convert UID bytes to a decimal integer string for the API.

        The API was originally built around numeric barcode IDs.
        RFID UIDs are sent as the equivalent decimal value of the
        big-endian unsigned integer formed by the UID bytes.

        Example: ``b'\\xa3\\xb7\\xc2\\xd4'`` → ``'2747777748'``
        """
        return str(int.from_bytes(uid_bytes, byteorder="big"))



    @staticmethod
    def hash_uid(uid: str, secret_key: str) -> str:
        """HMAC-SHA256 hash of the UID for secure logging.

        The raw UID is **never** written to logs — only this hash.
        """
        return hmac.new(
            secret_key.encode(), uid.encode(), hashlib.sha256,
        ).hexdigest()[:16]

    @staticmethod
    def detect_card_type(sak: bytes) -> CardType:
        """Detect MIFARE card type from the SAK (Select Acknowledge) byte."""
        if not sak:
            return CardType.UNKNOWN
        sak_val = sak[0] if isinstance(sak, (bytes, bytearray)) else sak
        if sak_val == 0x08:
            return CardType.MIFARE_1K
        if sak_val == 0x18:
            return CardType.MIFARE_4K
        if sak_val == 0x00:
            return CardType.MIFARE_ULTRALIGHT
        return CardType.UNKNOWN



# ════════════════════════════════════════════════════════════
# MANUAL UID INPUT HELPER
# ════════════════════════════════════════════════════════════


def _parse_uid_input(raw: str) -> Optional[bytes]:
    """Parse a user-typed UID string into bytes.

    Accepted formats (4-byte UIDs only):
      - Colon-separated hex : ``A3:B7:C2:D4``
      - Plain hex           : ``A3B7C2D4`` or ``a3b7c2d4``
      - Space-separated     : ``A3 B7 C2 D4``

    Returns ``bytes`` on success, ``None`` if the input is invalid.
    """
    cleaned = raw.strip().replace(":", "").replace(" ", "").replace("-", "")
    if len(cleaned) != 8:  # 4 bytes = 8 hex chars
        return None
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return None


# ════════════════════════════════════════════════════════════
# RFID READER — abstract base + concrete implementations
# ════════════════════════════════════════════════════════════


class RFIDReaderBase(abc.ABC):
    """Abstract base class for RFID/NFC readers.

    Subclass this to swap between RC522, PN532, or simulation readers
    without changing the rest of the application.
    """

    @abc.abstractmethod
    def initialize(self) -> bool:
        """Initialise hardware.  Returns ``True`` on success."""

    @abc.abstractmethod
    def read_card(self) -> Optional[RFIDCard]:
        """Non-blocking card read.  Returns ``None`` if no card present."""

    @abc.abstractmethod
    def reset(self) -> None:
        """Reset the reader (e.g. after an error)."""

    @abc.abstractmethod
    def cleanup(self) -> None:
        """Release hardware resources."""

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether the reader hardware is connected and ready."""


class RC522Reader(RFIDReaderBase):
    """MFRC522-based RFID reader via SPI on Raspberry Pi.

    SPI wiring (default)::

        SDA  → GPIO 8  (CE0)
        SCK  → GPIO 11 (SCLK)
        MOSI → GPIO 10
        MISO → GPIO 9
        RST  → GPIO 25  (configurable via ``rst_pin``)

    Falls back to unavailable state on non-RPi systems.
    """

    def __init__(self, rst_pin: int = 25, spi_bus: int = 0,
                 spi_device: int = 0) -> None:
        self._rst_pin = rst_pin
        self._spi_bus = spi_bus
        self._spi_device = spi_device
        self._reader: Any = None
        self._available = False

    def initialize(self) -> bool:
        if not _RFID_HW_AVAILABLE:
            log.warning("mfrc522 library not available — RC522 reader disabled")
            return False
        try:
            self._reader = MFRC522()
            self._available = True
            log.info(
                "RC522 RFID reader initialised (RST=GPIO%d, SPI %d:%d)",
                self._rst_pin, self._spi_bus, self._spi_device,
            )
            return True
        except Exception as exc:
            log.error("RC522 initialisation failed: %s", exc)
            self._available = False
            return False

    def read_card(self) -> Optional[RFIDCard]:
        if not self._available or self._reader is None:
            return None
        try:
            status, tag_type = self._reader.MFRC522_Request(
                self._reader.PICC_REQIDL,
            )
            if status != self._reader.MI_OK:
                return None

            status, raw_uid = self._reader.MFRC522_Anticoll()
            if status != self._reader.MI_OK:
                return None

            uid_bytes = bytes(raw_uid[:4])
            atqa = bytes([tag_type]) if isinstance(tag_type, int) else b"\x00"
            sak = bytes([raw_uid[4]]) if len(raw_uid) > 4 else b"\x00"

            # ---------------------------------------------------------
            # Read Sector 1, Block 4 for Programmed Student ID
            # ---------------------------------------------------------
            student_id = None
            try:
                self._reader.MFRC522_SelectTag(raw_uid)
                default_key = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
                # Authenticate Block 4
                status = self._reader.MFRC522_Auth(self._reader.PICC_AUTHENT1A, 4, default_key, raw_uid)
                if status == self._reader.MI_OK:
                    block_data = self._reader.MFRC522_Read(4)
                    if block_data:
                        # Convert bytes back to string and strip null bytes
                        parsed_str = bytes(block_data).partition(b'\x00')[0].decode('ascii', errors='ignore')
                        if parsed_str.isdigit() and len(parsed_str) > 3:
                            student_id = parsed_str
            except Exception as auth_exc:
                log.debug("Block 4 read failed: %s", auth_exc)

            # If a student ID was programmed, use it. Otherwise, fallback to the
            # decimal representation of the factory UID for the API.
            final_uid = student_id if student_id else UIDValidator.to_decimal(uid_bytes)

            card = RFIDCard(
                uid=final_uid,
                uid_bytes=uid_bytes,
                card_type=UIDValidator.detect_card_type(sak),
                atqa=atqa,
                sak=sak,
                read_timestamp=time.monotonic(),
            )

            # Put the card to sleep so it won't respond to REQIDL again
            # until physically removed and re-tapped. Without this the card
            # cycles back to IDLE state after ~1 s and gets re-processed.
            try:
                self._reader.MFRC522_StopCrypto1()
            except Exception:
                pass

            return card
        except Exception as exc:
            log.debug("RC522 read error: %s", exc)
            return None


    def reset(self) -> None:
        if self._reader is not None:
            try:
                self._reader.MFRC522_Init()
                log.debug("RC522 reset complete")
            except Exception as exc:
                log.warning("RC522 reset failed: %s", exc)

    def cleanup(self) -> None:
        self._available = False
        log.info("RC522 reader cleaned up")

    @property
    def is_available(self) -> bool:
        return self._available


class PN532Reader(RFIDReaderBase):
    """Stub for future PN532 NFC reader support.

    All methods raise ``NotImplementedError`` with guidance on
    what needs to be implemented.
    """

    def initialize(self) -> bool:
        raise NotImplementedError(
            "PN532 support is planned but not yet implemented. "
            "Use RFID_READER_TYPE=RC522 or SIMULATION_MODE=true."
        )

    def read_card(self) -> Optional[RFIDCard]:
        raise NotImplementedError("PN532 read_card not implemented")

    def reset(self) -> None:
        raise NotImplementedError("PN532 reset not implemented")

    def cleanup(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return False


class SimulationReader(RFIDReaderBase):
    """Simulated RFID reader for development without hardware.

    Generates fake UIDs at configurable intervals with a realistic
    scenario mix (configurable success rate).  All simulated reads
    are logged clearly with ``[SIMULATION]`` prefix.
    """

    def __init__(self, interval: float = 3.0,
                 success_rate: float = 0.8) -> None:
        self._interval = interval
        self._success_rate = success_rate
        self._available = False
        self._last_gen: float = 0.0
        # Pool of fake UIDs so denied cards get re-scanned sometimes
        self._uid_pool: list[bytes] = [
            bytes([random.randint(1, 255) for _ in range(4)])
            for _ in range(20)
        ]

    def initialize(self) -> bool:
        self._available = True
        log.info(
            "[SIMULATION] RFID reader active — interval=%.1fs, "
            "success_rate=%.0f%%",
            self._interval, self._success_rate * 100,
        )
        return True

    def read_card(self) -> Optional[RFIDCard]:
        if not self._available:
            return None
        now = time.monotonic()
        if now - self._last_gen < self._interval:
            return None
        self._last_gen = now

        uid_bytes = random.choice(self._uid_pool)
        sak = b"\x08"  # simulate MIFARE 1K
        card = RFIDCard(
            uid=UIDValidator.normalize(uid_bytes),
            uid_bytes=uid_bytes,
            card_type=CardType.MIFARE_1K,
            atqa=b"\x04\x00",
            sak=sak,
            read_timestamp=now,
        )
        log.debug("[SIMULATION] Generated card UID %s", card.uid)
        return card

    def reset(self) -> None:
        log.debug("[SIMULATION] Reader reset")

    def cleanup(self) -> None:
        self._available = False
        log.info("[SIMULATION] Reader cleaned up")

    @property
    def is_available(self) -> bool:
        return self._available


# ═══════════════════════════════════════════════════════════════════════════════
# CARD DETECTION MANAGER — anti-collision and debounce
# ═══════════════════════════════════════════════════════════════════════════════


class CardDetectionManager:
    """Prevents the same card from being processed more than once per debounce window.

    Uses per-UID timestamps instead of field-state tracking. This is more
    reliable with MIFARE readers because MFRC522_Request(REQIDL) only sees
    cards in IDLE state — after one read the card enters ACTIVE, briefly
    vanishes from the reader's perspective, then returns to IDLE. Field-state
    tracking would incorrectly reset and allow repeat processing. Time-based
    debouncing ignores all of that and simply enforces a minimum gap between
    processing the same UID.
    """

    def __init__(self, debounce_seconds: float = 3.0) -> None:
        self._debounce = debounce_seconds
        self._uid_times: dict[str, float] = {}
        self._lock = threading.Lock()

    def should_process(self, card: RFIDCard) -> bool:
        """Return True only if this UID hasn't been processed within the debounce window."""
        with self._lock:
            now = time.monotonic()
            last = self._uid_times.get(card.uid, 0.0)
            if now - last < self._debounce:
                return False
            self._uid_times[card.uid] = now
            return True

    def card_removed(self) -> None:
        """No-op — kept for API compatibility. Field tracking is no longer used."""

    def force_reset(self) -> None:
        """Clear all debounce state (call after reader errors to avoid stuck UIDs)."""
        with self._lock:
            self._uid_times.clear()



# ═══════════════════════════════════════════════════════════════════════════════
# GATE CONTROLLER — encapsulates Arduino Mega serial communication
# ═══════════════════════════════════════════════════════════════════════════════


class GateController:
    """
    Manages the Arduino Mega gate controller over USB serial.

    Arduino Mega Pin Layout::

        Pin 22  →  Solenoid Relay (Active HIGH)

    Features
    --------
    - Thread-safe command sending.
    - **Automatic reconnection** with exponential backoff when disconnected.
    - Background reader thread that parses Arduino status messages
      (``STATUS:*``, ``EMERGENCY:*``, acknowledgements).
    - Gate state tracking via :class:`GateStatus`.
    - Graceful shutdown (sends ``CLOSE`` to lock the solenoid before exit).

    The controller sends plain-text commands over USB serial::

        OPEN    — unlock solenoid (gate opens)
        CLOSE   — lock solenoid  (gate closes)
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
        self.send_command("CLOSE")
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

        1. Open gate  (with timeout-based OPEN confirmation check)
        2. Wait ``gate_open_duration`` seconds
        3. Close gate
        """
        def _run() -> None:
            self._set_gate_status(GateStatus.OPENING)

            ok = self.send_command("OPEN")
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
                log.error("OPEN command failed — possible hardware issue")

            time.sleep(self._config.gate_open_duration)

            self._set_gate_status(GateStatus.CLOSING)
            self.send_command("CLOSE")
            time.sleep(GATE_CLOSE_DELAY)
            self._set_gate_status(GateStatus.CLOSED)

        threading.Thread(target=_run, daemon=True, name="grant-seq").start()

    def deny_access_sequence(self) -> None:
        """No hardware action on deny. UI callback handles the visual update."""
        pass  # Gate does NOT open; display already shows denied state.

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
        """Parse and act on a status message received from the Arduino Mega."""
        log.debug("← Arduino: %s", msg)

        upper = msg.upper()

        # STATUS:* — gate status reports
        if upper.startswith("STATUS:"):
            value = upper.split(":", 1)[1].strip()
            if value == "OPEN":
                self._set_gate_status(GateStatus.OPEN)
            elif value == "CLOSED":
                self._set_gate_status(GateStatus.CLOSED)
            elif value == "ERROR":
                self._set_gate_status(GateStatus.ERROR)
                log.error("Arduino reports gate ERROR")

        # EMERGENCY:* — hardware emergency events
        elif upper.startswith("EMERGENCY:"):
            log.critical("EMERGENCY from Arduino: %s", msg)
            self._set_gate_status(GateStatus.ERROR)

        # Acknowledgement keywords
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
# STARTUP CONFIGURATION VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════


def validate_startup_config(config: GateConfig) -> list[str]:
    """
    Validate configuration at startup and return a list of warnings.

    Critical issues raise :class:`GateError`; non-critical ones are
    returned as warning strings for the caller to log.
    """
    warnings: list[str] = []

    # RFID reader type
    valid_readers = {"RC522", "PN532", "SIMULATION"}
    if config.rfid_reader_type not in valid_readers:
        raise GateError(
            f"RFID_READER_TYPE must be one of {valid_readers}, "
            f"got '{config.rfid_reader_type}'"
        )

    # SPI pins (GPIO range 0-27 for BCM)
    if not 0 <= config.rc522_rst_pin <= 27:
        warnings.append(
            f"RC522_RST_PIN={config.rc522_rst_pin} outside typical GPIO range (0-27)"
        )

    # HMAC key
    if config.uid_hash_key in ("", "change-me-generate-with-openssl"):
        warnings.append(
            "UID_HASH_KEY is not set — generate one with: openssl rand -hex 32"
        )

    # API URL scheme
    if config.api_url and not config.api_url.startswith("https://"):
        warnings.append(
            f"API URL is not HTTPS: {config.api_url} — consider using HTTPS"
        )

    # Simulation mode notice
    if config.simulation_mode:
        warnings.append(
            "SIMULATION_MODE is enabled — no real RFID hardware will be used"
        )

    return warnings


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
    are logged with the UID hash for auditing.

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
    def _key(identifier: str) -> str:
        """SHA-256 hash of the identifier (never store raw UIDs on disk)."""
        return hashlib.sha256(identifier.encode()).hexdigest()

    def store(self, uid: str, student: dict[str, Any]) -> None:
        """Cache a successful access check result."""
        if not self._enabled:
            return
        key = self._key(uid)
        with self._lock:
            self._cache[key] = (student, time.time())

    def lookup(self, uid: str) -> Optional[dict[str, Any]]:
        """Return cached student data if still valid, or ``None``."""
        if not self._enabled:
            return None
        key = self._key(uid)
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
    "failed_scans": 0,
    "avg_latency_ms": 0.0,
    "rfid_reader_type": "",
    "rfid_available": False,
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


# ═══════════════════════════
# ARABIC TEXT HELPER
# ═══════════════════════════


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


# ═══════════════════════════
# PHOTO LOADING HELPERS
# ═══════════════════════════


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


# ═════════════════════════════════════════════════════════════════════════════
# MAIN STUDENT CARD — the large card shown at the top of the screen
# ═════════════════════════════════════════════════════════════════════════════


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
        blank_pil = Image.new("RGB", MAIN_PHOTO_SIZE, Colors.PHOTO_BG)
        self._photo_ref = ImageTk.PhotoImage(blank_pil)
        self.photo_label.configure(image=self._photo_ref)
        for lbl in (
            self.status_label, self.name_label, self.seat_label,
            self.college_label, self.dept_label, self.time_label,
        ):
            lbl.configure(text="")


# ═════════════════════════════════════════════════════════════════════════════
# SMALL STUDENT CARD — history row at the bottom (previous entries)
# ═════════════════════════════════════════════════════════════════════════════


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
        blank_pil = Image.new("RGB", SMALL_PHOTO_SIZE, Colors.PHOTO_BG)
        self._photo_ref = ImageTk.PhotoImage(blank_pil)
        self.photo_label.configure(image=self._photo_ref)
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


# ═══════════════════════════════════════════════════════════════════
# HIGH-THROUGHPUT PROCESSOR — optimised for 6 000 students / day
# ═══════════════════════════════════════════════════════════════════


class HighThroughputProcessor:
    """Optimised scan-processing pipeline for 500-800 students/hour peaks.

    Architecture::

        [RFID Reader] → [CardDetectionManager] → [Queue] → [API Worker]
                                                              ↓
                                                        [GateController]
                                                        [GUI callback]

    - Detection loop polls the RFID reader and filters via CardDetectionManager.
    - API worker thread processes the queue with a connection-pooled session.
    - Rolling average latency tracking (last 100 scans).
    """

    def __init__(
        self,
        config: GateConfig,
        controller: GateController,
        reader: RFIDReaderBase,
        offline_cache: OfflineCache,
        ui_callback: Callable[[dict[str, Any], str], None],
    ) -> None:
        self._config = config
        self._controller = controller
        self._reader = reader
        self._offline_cache = offline_cache
        self._ui_callback = ui_callback  # GateApp._push_entry via after()

        self._detection_mgr = CardDetectionManager(config.card_debounce_seconds)
        self._queue: queue.Queue[RFIDCard] = queue.Queue(maxsize=10)
        self._shutdown = threading.Event()

        # Connection-pooled HTTP session
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=config.api_pool_size,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        if config.api_key:
            self._session.headers["Authorization"] = f"Bearer {config.api_key}"
        self._session.headers["Accept"] = "application/json"
        self._session.headers["Content-Type"] = "application/json"

        # Metrics
        self._latencies: collections.deque[float] = collections.deque(
            maxlen=ROLLING_LATENCY_WINDOW,
        )

    @property
    def metrics(self) -> dict[str, Any]:
        """Current performance metrics snapshot."""
        lats = list(self._latencies)
        return {
            "avg_latency_ms": round(sum(lats) / len(lats) * 1000, 1) if lats else 0.0,
        }

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch detection + worker threads."""
        threading.Thread(
            target=self._detection_loop, daemon=True, name="rfid-detect",
        ).start()
        
        # Spawn multiple API workers for true concurrent high-throughput
        num_workers = max(2, min(10, self._config.api_pool_size))
        for i in range(num_workers):
            threading.Thread(
                target=self._api_worker, daemon=True, name=f"api-worker-{i+1}",
            ).start()
            
        log.info("HighThroughputProcessor started with %d API workers", num_workers)

    def stop(self) -> None:
        self._shutdown.set()
        self._session.close()
        self._reader.cleanup()

    def inject_uid(self, uid_str: str) -> bool:
        """Inject a manually entered UID directly into the processing queue.

        Accepts hex strings in any common format: ``A3B7C2D4``,
        ``A3:B7:C2:D4``, or ``a3 b7 c2 d4``. Bypasses the debounce
        manager so the same card can be submitted multiple times from
        the terminal or GUI without waiting for the debounce window.

        Returns ``True`` if the card was queued, ``False`` on bad input
        or a full queue.
        """
        uid_bytes = _parse_uid_input(uid_str)
        if uid_bytes is None:
            log.warning("[MANUAL] Invalid UID format: '%s'", uid_str)
            return False
        if not UIDValidator.validate(uid_bytes):
            log.warning("[MANUAL] UID failed validation: '%s'", uid_str)
            return False
        card = RFIDCard(
            uid=UIDValidator.normalize(uid_bytes),
            uid_bytes=uid_bytes,
            card_type=CardType.UNKNOWN,
            atqa=b"\x00\x00",
            sak=b"\x00",
            read_timestamp=time.monotonic(),
        )
        log.info("[MANUAL] Injecting card UID %s", card.uid)
        try:
            self._queue.put_nowait(card)
            return True
        except queue.Full:
            log.warning("[MANUAL] Queue full — card dropped")
            return False



    def _detection_loop(self) -> None:
        """Poll the RFID reader; enqueue new cards for processing."""
        consecutive_errors = 0
        while not self._shutdown.is_set():
            try:
                card = self._reader.read_card()
                if card is None:
                    self._detection_mgr.card_removed()
                    time.sleep(RFID_POLL_INTERVAL)
                    consecutive_errors = 0
                    continue

                if not UIDValidator.validate(card.uid_bytes):
                    time.sleep(RFID_POLL_INTERVAL)
                    continue

                if self._detection_mgr.should_process(card):
                    uid_hash = UIDValidator.hash_uid(
                        card.uid, self._config.uid_hash_key,
                    )
                    log.info(
                        "Card detected [%s] type=%s hash=%s",
                        card.card_type.value, card.card_type.name, uid_hash,
                    )
                    try:
                        self._queue.put_nowait(card)
                    except queue.Full:
                        log.warning("Processing queue full — card dropped")

                consecutive_errors = 0
                time.sleep(RFID_POLL_INTERVAL)

            except Exception as exc:
                consecutive_errors += 1
                log.error("RFID detection error: %s", exc)
                if consecutive_errors >= 5:
                    log.warning("Too many errors — resetting reader")
                    self._reader.reset()
                    self._detection_mgr.force_reset()
                    consecutive_errors = 0
                time.sleep(0.5)

    # ── API worker ────────────────────────────────────────────────────────────

    def _api_worker(self) -> None:
        """Process queued cards: API call + gate control + UI update."""
        while not self._shutdown.is_set():
            try:
                card = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._process_card(card)

    def _process_card(self, card: RFIDCard) -> None:
        """Full scan pipeline for a single card."""
        start = time.monotonic()
        _health_state["total_scans"] += 1
        _health_state["last_scan_time"] = start

        # The UID is now either the programmed student seat number (e.g., '2420407')
        # or the fallback decimal conversion of the hardware UID bytes.
        uid_api = card.uid
        uid_hash = UIDValidator.hash_uid(card.uid, self._config.uid_hash_key)
        log.debug("Card UID decimal/student_id=%s hash=%s", uid_api, uid_hash)


        ssl_verify = self._config.ssl_verify
        max_retries = self._config.api_max_retries
        base_delay = self._config.api_retry_delay
        api_reachable = False

        for attempt in range(1, max_retries + 1):
            try:
                log.info(
                    "Verifying card hash=%s (attempt %d/%d)",
                    uid_hash, attempt, max_retries,
                )
                r = self._session.post(
                    self._config.api_url,
                    json={self._config.api_uid_field: uid_api},
                    timeout=self._config.api_timeout,
                    verify=ssl_verify,
                )

                # Non-retryable
                if r.status_code in (400, 401, 403, 404):
                    log.warning(
                        "API HTTP %d (non-retryable) hash=%s",
                        r.status_code, uid_hash,
                    )
                    break

                if r.status_code == 200:
                    api_reachable = True
                    _health_state["last_api_success_time"] = time.monotonic()

                    try:
                        data = r.json()
                        allowed, student = validate_api_response(data)
                    except (ValidationError, ValueError) as exc:
                        log.error("Malformed API response hash=%s: %s", uid_hash, exc)
                        break

                    if allowed and student:
                        log.info("ACCESS GRANTED — %s (hash=%s)",
                                 student.get("name", "?"), uid_hash)
                        _health_state["total_granted"] += 1
                        self._offline_cache.store(card.uid, student)
                        self._ui_callback(student, "granted")
                        self._controller.grant_access_sequence()
                        self._record_latency(start)
                        return

                    log.info("ACCESS DENIED by API — hash=%s", uid_hash)
                    break

                # 5xx retryable
                log.warning(
                    "API HTTP %d hash=%s (attempt %d/%d)",
                    r.status_code, uid_hash, attempt, max_retries,
                )

            except requests.exceptions.SSLError as exc:
                log.error("SSL error hash=%s: %s", uid_hash, exc)
                break

            except requests.exceptions.ConnectionError as exc:
                log.warning(
                    "Network unreachable hash=%s (attempt %d/%d): %s",
                    uid_hash, attempt, max_retries, exc,
                )

            except requests.exceptions.Timeout:
                log.warning(
                    "API timeout hash=%s (attempt %d/%d)",
                    uid_hash, attempt, max_retries,
                )

            except Exception as exc:
                log.error("Unexpected API error hash=%s: %s", uid_hash, exc)
                break

            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                log.debug("Retrying in %.1fs…", delay)
                time.sleep(delay)

        # All retries exhausted — try offline cache
        if not api_reachable:
            cached = self._offline_cache.lookup(card.uid)
            if cached:
                log.info("OFFLINE CACHE HIT (hash=%s) — granting access", uid_hash)
                _health_state["total_granted"] += 1
                self._ui_callback(cached, "granted")
                self._controller.grant_access_sequence()
                self._record_latency(start)
                return

        # Denied
        _health_state["total_denied"] += 1
        _health_state["failed_scans"] += 1
        denied_data: dict[str, Any] = {
            "name": "بطاقة غير صالحة",
            "seat_number": "",
            "college": "",
            "department": "",
        }
        self._ui_callback(denied_data, "denied")
        self._controller.deny_access_sequence()
        self._record_latency(start)

    def _record_latency(self, start: float) -> None:
        elapsed = time.monotonic() - start
        self._latencies.append(elapsed)
        lats = list(self._latencies)
        _health_state["avg_latency_ms"] = round(
            sum(lats) / len(lats) * 1000, 1,
        ) if lats else 0.0


# ════════════════════
# MAIN APPLICATION
# ════════════════════


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
    """

    MAX_HISTORY: int = 3

    def __init__(
        self,
        config: GateConfig,
        controller: GateController,
        rfid_reader: RFIDReaderBase,
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

        # ── UI ────────────────────────────────────────────────────────────────
        self._build_header()
        self._build_content()
        self._build_status_bar()

        # ── periodic updates ──────────────────────────────────────────────────
        self._update_arduino_indicator()

        # ── start RFID processor ──────────────────────────────────────────────
        self._processor = HighThroughputProcessor(
            config, controller, rfid_reader, self._offline_cache,
            ui_callback=self._schedule_push_entry,
        )
        self._processor.start()
        self.bind("<Control-m>", lambda _e: self._show_manual_input_dialog())
        log.info("GUI started — waiting for RFID scans")


    # ════════════════════
    # UI CONSTRUCTION
    # ════════════════════

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

        # Manual input button — opens a dialog for typing a card UID
        ctk.CTkButton(
            hc,
            text="⌨",
            width=44,
            height=44,
            corner_radius=8,
            fg_color=Colors.TIME_BG,
            hover_color=Colors.CARD_BORDER,
            font=ctk.CTkFont(size=22),
            command=self._show_manual_input_dialog,
        ).pack(side="right", padx=(0, 12))

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

    # ════════════════════
    # PERIODIC UI UPDATES
    # ════════════════════

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
        else:
            blank_pil = Image.new("RGB", MAIN_PHOTO_SIZE, Colors.PHOTO_BG)
            self.main_card._photo_ref = ImageTk.PhotoImage(blank_pil)
            self.main_card.photo_label.configure(image=self.main_card._photo_ref)

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

    # ═════════════════════════════════
    # THREAD-SAFE UI CALLBACK
    # ═════════════════════════════════

    def _schedule_push_entry(
        self, student: dict[str, Any], status: str,
    ) -> None:
        """Schedule ``_push_entry`` on the Tk main thread (thread-safe)."""
        self.after(0, lambda s=student, st=status: self._push_entry(s, st))

    # ═════════════════════════════════
    # MANUAL UID INPUT
    # ═════════════════════════════════

    def _show_manual_input_dialog(self) -> None:
        """Open a dialog to manually enter a card UID.

        Useful for testing without a physical card, or for overriding
        access during maintenance. The entered UID goes through the same
        API verification pipeline as a real scan.

        Keyboard shortcut: Ctrl+M.
        """
        dialog = ctk.CTkInputDialog(
            text="Enter card UID and press OK.\n\nFormats: A3B7C2D4  or  A3:B7:C2:D4",
            title="Manual Card Entry",
        )
        uid_str = dialog.get_input()
        if not uid_str or not uid_str.strip():
            return

        ok = self._processor.inject_uid(uid_str.strip())
        if not ok:
            self._status_label.configure(
                text=f"Invalid UID: {uid_str.strip()!r}  (expected 8 hex chars)",
                text_color=Colors.ORANGE,
            )
            self.after(STATUS_BAR_RESET_MS, self._reset_status_bar)



# ═══════════════════════════
# RFID READER FACTORY
# ═══════════════════════════


def _create_rfid_reader(config: GateConfig) -> RFIDReaderBase:
    """Instantiate the correct RFID reader based on configuration."""
    if config.simulation_mode or config.rfid_reader_type == "SIMULATION":
        reader = SimulationReader(
            interval=config.simulation_interval,
            success_rate=config.simulation_success_rate,
        )
    elif config.rfid_reader_type == "RC522":
        reader = RC522Reader(
            rst_pin=config.rc522_rst_pin,
            spi_bus=config.rc522_spi_bus,
            spi_device=config.rc522_spi_device,
        )
    elif config.rfid_reader_type == "PN532":
        reader = PN532Reader()
    else:
        raise GateError(f"Unknown RFID reader type: {config.rfid_reader_type}")

    if not reader.initialize():
        log.warning(
            "RFID reader '%s' unavailable — hardware scans disabled. "
            "You can still use manual input.",
            config.rfid_reader_type,
        )


    return reader


# ═════════════════
# ENTRY POINT
# ═════════════════

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

    try:
        startup_warnings = validate_startup_config(cfg)
        for w in startup_warnings:
            log.warning("CONFIG: %s", w)
    except GateError as e:
        log.critical("Configuration validation failed: %s", e)
        sys.exit(1)

    log.info("API URL         : %s", cfg.api_url)
    log.info("API key         : %s", "configured" if cfg.api_key else "NOT SET")
    log.info("API UID field   : %s", cfg.api_uid_field)
    log.info("SSL verify      : %s", cfg.ssl_verify)
    log.info("Serial port     : %s @ %d baud", cfg.serial_port, cfg.baud_rate)
    log.info("RFID reader     : %s", cfg.rfid_reader_type)
    log.info("Gate open       : %.1fs", cfg.gate_open_duration)
    log.info("Card debounce   : %.1fs", cfg.card_debounce_seconds)
    log.info("Pool size       : %d", cfg.api_pool_size)
    log.info("Offline mode    : %s (TTL %ds)", cfg.offline_mode, int(cfg.offline_cache_ttl))
    log.info("Simulation mode : %s", cfg.simulation_mode)
    log.info("Health port     : %s", cfg.health_port or "disabled")

    # ── Initialise RFID reader ────────────────────────────────────────────────
    rfid_reader = _create_rfid_reader(cfg)
    _health_state["rfid_reader_type"] = cfg.rfid_reader_type
    _health_state["rfid_available"] = rfid_reader.is_available

    # ── Initialise gate controller ────────────────────────────────────────────
    gate_ctrl = GateController(cfg)
    gate_ctrl.start()

    # ── Start health-check server (if configured) ─────────────────────────────
    health_srv: Optional[HealthCheckServer] = None
    if cfg.health_port > 0:
        health_srv = HealthCheckServer(cfg.health_port, gate_ctrl)
        health_srv.start()

    # ── Launch the GUI ────────────────────────────────────────────────────────
    app = GateApp(cfg, gate_ctrl, rfid_reader)

    # ── Terminal manual input thread ──────────────────────────────────────────
    def _stdin_reader(processor: HighThroughputProcessor) -> None:
        """Read UIDs typed in the terminal and inject them for processing."""
        sys.stdout.write(
            "\n[Manual Input] Type a card UID and press Enter to simulate a scan.\n"
            "[Manual Input] Formats: A3B7C2D4  or  A3:B7:C2:D4\n\n"
        )
        sys.stdout.flush()
        while True:
            try:
                raw = input()
                if not raw.strip():
                    continue
                ok = processor.inject_uid(raw.strip())
                msg = (
                    f"[Manual Input] Queued: {raw.strip()}\n"
                    if ok
                    else f"[Manual Input] Invalid UID: {raw.strip()!r}  (expected 8 hex chars)\n"
                )
                sys.stdout.write(msg)
                sys.stdout.flush()
            except EOFError:
                # stdin closed — happens when running via systemd / nohup
                break
            except Exception:
                break

    threading.Thread(
        target=_stdin_reader,
        args=(app._processor,),
        daemon=True,
        name="stdin-manual",
    ).start()

    try:
        app.mainloop()

    finally:
        log.info("Shutting down…")
        if hasattr(app, "_processor"):
            app._processor.stop()
        gate_ctrl.shutdown()
        if health_srv:
            health_srv.stop()
        log.info("══════════════════════════════════════════════════")
        log.info(" Gate Access Monitoring System — stopped")
        log.info("══════════════════════════════════════════════════")