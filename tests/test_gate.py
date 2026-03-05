"""
Unit tests for the Gate Access Monitoring System.

Run with::

    pytest tests/ -v
    pytest tests/ -v --cov=gate --cov-report=term-missing
"""
from __future__ import annotations

import hashlib
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test.  We patch heavy third-party dependencies
# so the test suite runs without customtkinter, serial, or a display.
# --------------------------------------------------------------------------
# We can't import gate.py at module level because it calls
# ``ctk.set_appearance_mode`` and sets up logging handlers at import time.
# Instead, we import specific symbols inside each test or use importlib.
# For the pure utility functions and data classes, we can safely import
# after stubbing the heavy libraries.
# --------------------------------------------------------------------------

import importlib
import sys
import types


@pytest.fixture(scope="session", autouse=True)
def _stub_heavy_libs():
    """
    Insert lightweight stubs for third-party packages that require a display
    or hardware (customtkinter, serial, arabic_reshaper, bidi, PIL).

    This lets us import ``gate`` purely for testing logic functions.
    """
    # ── customtkinter stub ────────────────────────────────────────────────────
    ctk_stub = types.ModuleType("customtkinter")
    ctk_stub.set_appearance_mode = lambda _: None
    ctk_stub.set_default_color_theme = lambda _: None
    ctk_stub.CTk = type("CTk", (), {"__init__": lambda *a, **kw: None})
    ctk_stub.CTkFrame = type("CTkFrame", (), {"__init__": lambda *a, **kw: None})
    ctk_stub.CTkLabel = type("CTkLabel", (), {"__init__": lambda *a, **kw: None})
    ctk_stub.CTkFont = lambda **kw: None
    ctk_stub.CTkBaseClass = type("CTkBaseClass", (), {})
    sys.modules["customtkinter"] = ctk_stub

    # ── serial stub ───────────────────────────────────────────────────────────
    serial_stub = types.ModuleType("serial")
    serial_stub.Serial = MagicMock
    serial_stub.SerialException = type("SerialException", (Exception,), {})
    sys.modules["serial"] = serial_stub

    # ── arabic_reshaper stub ──────────────────────────────────────────────────
    ar_stub = types.ModuleType("arabic_reshaper")
    ar_stub.reshape = lambda text: text
    sys.modules["arabic_reshaper"] = ar_stub

    # ── bidi stub ─────────────────────────────────────────────────────────────
    bidi_stub = types.ModuleType("bidi")
    bidi_algo = types.ModuleType("bidi.algorithm")
    bidi_algo.get_display = lambda text: text
    bidi_stub.algorithm = bidi_algo
    sys.modules["bidi"] = bidi_stub
    sys.modules["bidi.algorithm"] = bidi_algo

    # ── PIL stub ──────────────────────────────────────────────────────────────
    pil_stub = types.ModuleType("PIL")
    image_stub = types.ModuleType("PIL.Image")

    class _FakeImage:
        Resampling = type("Resampling", (), {"LANCZOS": 1})()

        def __init__(self, width: int = 100, height: int = 100):
            self.width = width
            self.height = height

        def crop(self, box):
            return _FakeImage(box[2] - box[0], box[3] - box[1])

        def resize(self, size, _):
            return _FakeImage(size[0], size[1])

        def convert(self, _):
            return self

        @staticmethod
        def open(_):
            return _FakeImage()

    image_stub.Image = _FakeImage
    image_stub.open = _FakeImage.open

    imagetk_stub = types.ModuleType("PIL.ImageTk")
    imagetk_stub.PhotoImage = MagicMock

    pil_stub.Image = image_stub
    pil_stub.ImageTk = imagetk_stub
    sys.modules["PIL"] = pil_stub
    sys.modules["PIL.Image"] = image_stub
    sys.modules["PIL.ImageTk"] = imagetk_stub

    yield

    # Teardown — not strictly necessary since tests run and exit.
    for mod in (
        "customtkinter", "serial", "arabic_reshaper",
        "bidi", "bidi.algorithm", "PIL", "PIL.Image", "PIL.ImageTk",
    ):
        sys.modules.pop(mod, None)


@pytest.fixture()
def gate_module():
    """Import (or re-import) the gate module with stubs already in place."""
    # Force re-import so patches take effect cleanly
    sys.modules.pop("gate", None)
    # Set a required env var so GateConfig.from_env() doesn't fail at import
    import os
    os.environ.setdefault("GATE_API_URL", "https://test.example.com/api/check")
    mod = importlib.import_module("gate")
    return mod


# ═══════════════════════════════════════════════════════════════════════════════
# validate_barcode()
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateBarcode:
    """Tests for the barcode input-validation function."""

    def test_valid_numeric(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("1234567890") is True

    def test_valid_alphanumeric(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("ABC-123_test.01") is True

    def test_empty_string(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("") is False

    def test_too_long(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("A" * 51) is False

    def test_exactly_max_length(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("A" * 50) is True

    def test_special_characters_rejected(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("12345; DROP TABLE") is False

    def test_unicode_rejected(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("مرحبا") is False

    def test_newline_rejected(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("abc\ndef") is False

    def test_html_injection_rejected(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("<script>alert(1)</script>") is False

    def test_backslash_rejected(self, gate_module: types.ModuleType) -> None:
        assert gate_module.validate_barcode("abc\\def") is False


# ═══════════════════════════════════════════════════════════════════════════════
# validate_api_response()
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateApiResponse:
    """Tests for the API response validation function."""

    def test_granted_response(self, gate_module: types.ModuleType) -> None:
        data = {
            "data": {
                "allowed": True,
                "student": {"name": "Ahmad", "seat_number": "123"},
            }
        }
        allowed, student = gate_module.validate_api_response(data)
        assert allowed is True
        assert student["name"] == "Ahmad"

    def test_denied_response(self, gate_module: types.ModuleType) -> None:
        data = {"data": {"allowed": False, "student": None}}
        allowed, student = gate_module.validate_api_response(data)
        assert allowed is False
        assert student == {}

    def test_missing_data_key(self, gate_module: types.ModuleType) -> None:
        with pytest.raises(gate_module.ValidationError, match="'data'"):
            gate_module.validate_api_response({"result": True})

    def test_allowed_not_bool(self, gate_module: types.ModuleType) -> None:
        with pytest.raises(gate_module.ValidationError, match="boolean"):
            gate_module.validate_api_response({"data": {"allowed": "yes"}})

    def test_student_not_dict(self, gate_module: types.ModuleType) -> None:
        with pytest.raises(gate_module.ValidationError, match="dict"):
            gate_module.validate_api_response(
                {"data": {"allowed": True, "student": "bad"}},
            )

    def test_student_missing_name(self, gate_module: types.ModuleType) -> None:
        with pytest.raises(gate_module.ValidationError, match="name"):
            gate_module.validate_api_response(
                {"data": {"allowed": True, "student": {"seat_number": "1"}}},
            )

    def test_not_a_dict(self, gate_module: types.ModuleType) -> None:
        with pytest.raises(gate_module.ValidationError, match="dict"):
            gate_module.validate_api_response("not a dict")

    def test_none_input(self, gate_module: types.ModuleType) -> None:
        with pytest.raises(gate_module.ValidationError):
            gate_module.validate_api_response(None)


# ═══════════════════════════════════════════════════════════════════════════════
# OfflineCache
# ═══════════════════════════════════════════════════════════════════════════════


class TestOfflineCache:
    """Tests for the time-limited offline access cache."""

    def test_disabled_cache_returns_none(self, gate_module: types.ModuleType) -> None:
        cache = gate_module.OfflineCache(enabled=False)
        cache.store("123", {"name": "Test"})
        assert cache.lookup("123") is None

    def test_store_and_lookup(self, gate_module: types.ModuleType) -> None:
        cache = gate_module.OfflineCache(ttl=60.0, enabled=True)
        student = {"name": "Ahmad", "seat_number": "456"}
        cache.store("123", student)
        result = cache.lookup("123")
        assert result is not None
        assert result["name"] == "Ahmad"

    def test_expired_entry_returns_none(self, gate_module: types.ModuleType) -> None:
        cache = gate_module.OfflineCache(ttl=0.01, enabled=True)
        cache.store("123", {"name": "Test"})
        time.sleep(0.02)
        assert cache.lookup("123") is None

    def test_prune_removes_expired(self, gate_module: types.ModuleType) -> None:
        cache = gate_module.OfflineCache(ttl=0.01, enabled=True)
        cache.store("111", {"name": "A"})
        cache.store("222", {"name": "B"})
        time.sleep(0.02)
        cache.prune()
        assert cache.lookup("111") is None
        assert cache.lookup("222") is None

    def test_stores_by_hash_not_raw(self, gate_module: types.ModuleType) -> None:
        """Ensure the cache key is a SHA-256 hash, not the raw barcode."""
        cache = gate_module.OfflineCache(ttl=60.0, enabled=True)
        cache.store("secret-barcode", {"name": "Student"})
        expected_key = hashlib.sha256(b"secret-barcode").hexdigest()
        assert expected_key in cache._cache
        assert "secret-barcode" not in cache._cache


# ═══════════════════════════════════════════════════════════════════════════════
# reshape_arabic()
# ═══════════════════════════════════════════════════════════════════════════════


class TestReshapeArabic:
    """Tests for the Arabic text reshaping helper."""

    def test_empty_string(self, gate_module: types.ModuleType) -> None:
        assert gate_module.reshape_arabic("") == ""

    def test_none_returns_empty(self, gate_module: types.ModuleType) -> None:
        # The function checks ``if not text`` which handles None
        assert gate_module.reshape_arabic(None) == ""  # type: ignore[arg-type]

    def test_ascii_passthrough(self, gate_module: types.ModuleType) -> None:
        # With stubs, reshape and get_display are identity functions
        assert gate_module.reshape_arabic("Hello") == "Hello"

    def test_arabic_text(self, gate_module: types.ModuleType) -> None:
        # Stubs return input unchanged — just ensure no crash
        result = gate_module.reshape_arabic("مرحبا")
        assert isinstance(result, str)
        assert len(result) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# resolve_photo_url()
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolvePhotoUrl:
    """Tests for photo URL resolution."""

    def test_relative_path(self, gate_module: types.ModuleType) -> None:
        result = gate_module.resolve_photo_url("/photos/a.jpg", "https://example.com")
        assert result == "https://example.com/photos/a.jpg"

    def test_absolute_url_unchanged(self, gate_module: types.ModuleType) -> None:
        url = "https://cdn.example.com/photo.jpg"
        assert gate_module.resolve_photo_url(url, "https://other.com") == url

    def test_empty_url(self, gate_module: types.ModuleType) -> None:
        assert gate_module.resolve_photo_url("", "https://example.com") == ""

    def test_none_url(self, gate_module: types.ModuleType) -> None:
        assert gate_module.resolve_photo_url(None, "https://example.com") == ""  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════════
# GateConfig
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateConfig:
    """Tests for configuration loading and validation."""

    def test_from_env_missing_api_url(self, gate_module: types.ModuleType) -> None:
        """GATE_API_URL must be set."""
        import os
        env = os.environ.copy()
        env.pop("GATE_API_URL", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(gate_module.GateError, match="GATE_API_URL"):
                gate_module.GateConfig.from_env()

    def test_from_env_valid(self, gate_module: types.ModuleType) -> None:
        import os
        env = {
            "GATE_API_URL": "https://test.example.com/api",
            "GATE_API_KEY": "secret-key",
            "GATE_SERIAL_PORT": "/dev/ttyUSB0",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = gate_module.GateConfig.from_env()
            assert cfg.api_url == "https://test.example.com/api"
            assert cfg.api_key == "secret-key"
            assert cfg.serial_port == "/dev/ttyUSB0"
            assert cfg.verify_ssl is True  # default

    def test_from_env_negative_gate_duration(self, gate_module: types.ModuleType) -> None:
        import os
        env = {"GATE_API_URL": "https://x.com", "GATE_OPEN_DURATION": "-1"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(gate_module.GateError, match="GATE_OPEN_DURATION"):
                gate_module.GateConfig.from_env()

    def test_ssl_verify_property(self, gate_module: types.ModuleType) -> None:
        import os
        env = {"GATE_API_URL": "https://x.com", "GATE_VERIFY_SSL": "false"}
        with patch.dict(os.environ, env, clear=True):
            cfg = gate_module.GateConfig.from_env()
            assert cfg.ssl_verify is False

    def test_ssl_verify_with_cert(self, gate_module: types.ModuleType, tmp_path) -> None:
        import os
        cert = tmp_path / "ca.pem"
        cert.write_text("fake cert")
        env = {
            "GATE_API_URL": "https://x.com",
            "GATE_API_CERT_PATH": str(cert),
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = gate_module.GateConfig.from_env()
            assert cfg.ssl_verify == str(cert)


# ═══════════════════════════════════════════════════════════════════════════════
# GateStatus enum
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateStatus:
    """Ensure the GateStatus enum has all expected members."""

    def test_all_statuses_exist(self, gate_module: types.ModuleType) -> None:
        GS = gate_module.GateStatus
        names = {s.name for s in GS}
        assert "UNKNOWN" in names
        assert "OPEN" in names
        assert "CLOSED" in names
        assert "ERROR" in names
        assert "OCCUPIED" in names

    def test_value_strings(self, gate_module: types.ModuleType) -> None:
        assert gate_module.GateStatus.OPEN.value == "open"
        assert gate_module.GateStatus.CLOSED.value == "closed"


# ═══════════════════════════════════════════════════════════════════════════════
# Custom Exceptions
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    """Verify the custom exception hierarchy."""

    def test_hierarchy(self, gate_module: types.ModuleType) -> None:
        assert issubclass(gate_module.ArduinoError, gate_module.GateError)
        assert issubclass(gate_module.APIError, gate_module.GateError)
        assert issubclass(gate_module.ValidationError, gate_module.GateError)

    def test_gate_error_is_exception(self, gate_module: types.ModuleType) -> None:
        assert issubclass(gate_module.GateError, Exception)
