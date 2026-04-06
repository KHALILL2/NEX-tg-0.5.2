"""
Security verification tests for barcode masking in logs.
"""
from __future__ import annotations

import hashlib
import logging
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

@pytest.fixture(scope="module", autouse=True)
def _stub_libs():
    """Stub heavy libraries for gate import."""
    stubs = [
        "customtkinter", "serial", "arabic_reshaper",
        "bidi", "bidi.algorithm", "PIL", "PIL.Image", "PIL.ImageTk",
        "requests", "requests.exceptions"
    ]
    for mod_name in stubs:
        mock_mod = types.ModuleType(mod_name)
        if mod_name == "customtkinter":
            mock_mod.set_appearance_mode = lambda _: None
            mock_mod.set_default_color_theme = lambda _: None

            class MockCTk:
                def __init__(self, *a, **kw): pass
                def title(self, *a): pass
                def attributes(self, *a, **kw): pass
                def configure(self, *a, **kw): pass
                def after(self, *a, **kw): pass
                def mainloop(self): pass

            mock_mod.CTk = MockCTk
            mock_mod.CTkFrame = type("CTkFrame", (), {"__init__": lambda *a, **kw: None, "pack": lambda *a, **kw: None, "pack_propagate": lambda *a, **kw: None})
            mock_mod.CTkLabel = type("CTkLabel", (), {"__init__": lambda *a, **kw: None, "pack": lambda *a, **kw: None, "configure": lambda *a, **kw: None})
            mock_mod.CTkFont = lambda **kw: None
            mock_mod.CTkBaseClass = type("CTkBaseClass", (), {})
        elif mod_name == "requests":
            mock_mod.post = MagicMock()
            mock_mod.exceptions = types.ModuleType("requests.exceptions")
            mock_mod.exceptions.SSLError = type("SSLError", (Exception,), {})
            mock_mod.exceptions.ConnectionError = type("ConnectionError", (Exception,), {})
            mock_mod.exceptions.Timeout = type("Timeout", (Exception,), {})
            sys.modules["requests.exceptions"] = mock_mod.exceptions
        elif mod_name == "bidi.algorithm":
            mock_mod.get_display = lambda text: text

        sys.modules[mod_name] = mock_mod

    yield

    for mod_name in stubs:
        sys.modules.pop(mod_name, None)

@pytest.fixture
def gate():
    """Import gate module."""
    if "gate" in sys.modules:
        del sys.modules["gate"]
    import os
    os.environ["GATE_API_URL"] = "http://api"
    import gate
    return gate

def test_validate_barcode_logging_masks_too_long(gate, caplog):
    """Ensure too-long barcodes are masked in logs."""
    caplog.set_level(logging.WARNING)
    long_code = "A" * 100
    gate.validate_barcode(long_code)

    masked = gate._mask_barcode(long_code)
    assert long_code not in caplog.text
    assert masked in caplog.text

def test_validate_barcode_logging_masks_invalid_chars(gate, caplog):
    """Ensure barcodes with invalid characters are masked in logs."""
    caplog.set_level(logging.WARNING)
    invalid_code = "abc;drop"
    gate.validate_barcode(invalid_code)

    masked = gate._mask_barcode(invalid_code)
    assert invalid_code not in caplog.text
    assert masked in caplog.text

def test_process_scan_logging_masks_barcode(gate, caplog):
    """Ensure _process_scan masks barcodes in all log levels."""
    caplog.set_level(logging.INFO)
    code = "secret123"
    masked = gate._mask_barcode(code)

    # Setup config and app to call _process_scan
    mock_config = MagicMock()
    mock_config.api_max_retries = 1
    mock_config.api_url = "http://api"
    mock_config.api_key = None
    mock_config.ssl_verify = True
    mock_config.api_timeout = 1
    mock_config.offline_mode = False
    mock_config.offline_cache_ttl = 300

    mock_controller = MagicMock()

    app = gate.GateApp(mock_config, mock_controller)

    # Mock requests.post to return 404 to trigger a warning log
    import requests
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    requests.post.return_value = mock_resp

    app._process_scan(code)

    assert code not in caplog.text
    assert masked in caplog.text
    assert f"Scanning barcode: {masked}" in caplog.text
    assert f"API HTTP 404 (non-retryable) for barcode {masked}" in caplog.text

def test_handle_code_debounce_logging_masks_barcode(gate, caplog):
    """Ensure debounce logging masks barcodes."""
    caplog.set_level(logging.DEBUG)
    code = "debounce-me"
    masked = gate._mask_barcode(code)

    mock_config = MagicMock()
    mock_config.debounce_seconds = 10
    mock_config.offline_mode = False
    mock_config.offline_cache_ttl = 300
    mock_controller = MagicMock()

    app = gate.GateApp(mock_config, mock_controller)

    # First scan
    app._handle_code(code)
    # Second scan (immediate)
    app._handle_code(code)

    assert code not in caplog.text
    assert masked in caplog.text
    assert "Debounce: ignored duplicate" in caplog.text
    assert masked in caplog.text
