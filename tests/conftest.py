import importlib
import sys
import types
from unittest.mock import MagicMock, patch
import pytest

@pytest.fixture(scope="session", autouse=True)
def _stub_heavy_libs():
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
            self.width, self.height = width, height
        def crop(self, box): return _FakeImage(box[2] - box[0], box[3] - box[1])
        def resize(self, size, _): return _FakeImage(size[0], size[1])
        def convert(self, _): return self
        @staticmethod
        def open(_): return _FakeImage()

    image_stub.Image = _FakeImage
    image_stub.open = _FakeImage.open
    imagetk_stub = types.ModuleType("PIL.ImageTk")
    imagetk_stub.PhotoImage = MagicMock
    pil_stub.Image, pil_stub.ImageTk = image_stub, imagetk_stub
    sys.modules["PIL"] = pil_stub
    sys.modules["PIL.Image"] = image_stub
    sys.modules["PIL.ImageTk"] = imagetk_stub

    # ── requests stub ─────────────────────────────────────────────────────────
    requests_stub = types.ModuleType("requests")
    requests_stub.get = MagicMock()
    requests_stub.post = MagicMock()
    requests_stub.exceptions = types.ModuleType("requests.exceptions")
    requests_stub.exceptions.SSLError = type("SSLError", (Exception,), {})
    requests_stub.exceptions.ConnectionError = type("ConnectionError", (Exception,), {})
    requests_stub.exceptions.Timeout = type("Timeout", (Exception,), {})
    sys.modules["requests"] = requests_stub
    sys.modules["requests.exceptions"] = requests_stub.exceptions

    yield

@pytest.fixture()
def gate_module():
    sys.modules.pop("gate", None)
    import os
    os.environ.setdefault("GATE_API_URL", "https://test.example.com/api/check")
    mod = importlib.import_module("gate")
    return mod
