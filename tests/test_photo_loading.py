from unittest.mock import MagicMock, patch
import pytest

def test_load_photo_async_skips_localhost(gate_module):
    # Setup
    mock_widget = MagicMock()
    mock_callback = MagicMock()

    with patch("threading.Thread") as mock_thread:
        # Test localhost
        gate_module.load_photo_async("http://localhost/photo.jpg", (100, 100), mock_widget, mock_callback)
        mock_thread.assert_not_called()

        # Test 127.0.0.1
        gate_module.load_photo_async("http://127.0.0.1/photo.jpg", (100, 100), mock_widget, mock_callback)
        mock_thread.assert_not_called()

        # Test normal URL
        gate_module.load_photo_async("http://example.com/photo.jpg", (100, 100), mock_widget, mock_callback)
        assert mock_thread.called

def test_load_photo_async_skips_empty_url(gate_module):
    mock_widget = MagicMock()
    mock_callback = MagicMock()

    with patch("threading.Thread") as mock_thread:
        gate_module.load_photo_async("", (100, 100), mock_widget, mock_callback)
        mock_thread.assert_not_called()

        gate_module.load_photo_async(None, (100, 100), mock_widget, mock_callback)
        mock_thread.assert_not_called()
