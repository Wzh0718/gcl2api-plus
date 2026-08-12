from pathlib import Path


COMMON_JS = Path(__file__).resolve().parents[1] / "front" / "common.js"


def test_log_websocket_reuses_connecting_or_open_socket():
    source = COMMON_JS.read_text(encoding="utf-8")
    assert "WebSocket.CONNECTING" in source
    assert "WebSocket.OPEN" in source


def test_log_websocket_is_closed_when_leaving_logs_or_unloading_page():
    source = COMMON_JS.read_text(encoding="utf-8")
    assert "currentContent?.id === 'logsTab'" in source
    assert "window.addEventListener('pagehide', disconnectWebSocket)" in source


def test_stale_websocket_callbacks_cannot_overwrite_current_connection():
    source = COMMON_JS.read_text(encoding="utf-8")
    assert "AppState.logWebSocket !== socket" in source
