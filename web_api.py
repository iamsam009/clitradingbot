"""
web_api.py - Lightweight HTTP API + Dashboard Server

Provides a JSON REST API and serves the web dashboard.
Uses Python's built-in http.server — zero extra dependencies.

Endpoints:
  GET  /              → serves dashboard.html
  GET  /api/status    → full bot state as JSON
  POST /api/config    → update bot settings
"""

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional, Callable
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("web_api")

# ---------------------------------------------------------------------------
#  Shared state (written by bot, read by API handler)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state: Dict[str, Any] = {}
_config_callback: Optional[Callable[[str, Any], None]] = None
_dashboard_path: str = ""


def update_state(data: Dict[str, Any]):
    """Called by the bot each cycle to push the latest state."""
    with _state_lock:
        _state.clear()
        _state.update(data)


def set_config_callback(cb: Callable[[str, Any], None]):
    """Register a callback for config changes from the dashboard."""
    global _config_callback
    _config_callback = cb


def set_dashboard_path(path: str):
    """Set the path to dashboard.html."""
    global _dashboard_path
    _dashboard_path = path


# ---------------------------------------------------------------------------
#  HTTP Handler
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """Handles API and static file requests."""

    def log_message(self, format, *args):
        """Suppress default logging to stderr."""
        logger.debug(format % args)

    # ── Routing ──

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/status":
            self._serve_status()
        elif path == "/" or path == "/dashboard":
            self._serve_dashboard()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/config":
            self._handle_config_update()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ── Helpers ──

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # ── Endpoints ──

    def _serve_status(self):
        with _state_lock:
            data = dict(_state)
        self._send_json(data)

    def _serve_dashboard(self):
        try:
            if os.path.isfile(_dashboard_path):
                with open(_dashboard_path, "r", encoding="utf-8") as f:
                    html = f.read()
                self._serve_html(html)
            else:
                self._send_json(
                    {"error": "dashboard.html not found", "path": _dashboard_path}, 500
                )
        except Exception as e:
            logger.error(f"Failed to serve dashboard: {e}")
            self._send_json({"error": str(e)}, 500)

    def _handle_config_update(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
        except Exception as e:
            self._send_json({"ok": False, "error": f"Invalid JSON: {e}"}, 400)
            return

        key = data.get("key", "")
        value = data.get("value")

        if not key:
            self._send_json({"ok": False, "error": "Missing 'key' field"}, 400)
            return

        logger.info(f"Config update via dashboard: {key} = {value}")

        if _config_callback:
            try:
                _config_callback(key, value)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
                return

        self._send_json({"ok": True, "key": key, "value": value})


# ---------------------------------------------------------------------------
#  Server lifecycle
# ---------------------------------------------------------------------------

_server: Optional[HTTPServer] = None
_server_thread: Optional[threading.Thread] = None


def start_server(host: str = "0.0.0.0", port: int = 8080) -> bool:
    """
    Start the HTTP server in a background daemon thread.

    Returns True if started successfully, False otherwise.
    """
    global _server, _server_thread

    # Resolve dashboard path — same directory as this file
    import inspect

    try:
        frame = inspect.currentframe()
        if frame:
            src = frame.f_code.co_filename
        else:
            src = __file__
    except Exception:
        src = __file__
    if not src or src == "<string>":
        src = os.path.join(os.getcwd(), "web_api.py")
    set_dashboard_path(os.path.join(os.path.dirname(os.path.abspath(src)), "dashboard.html"))

    try:
        _server = HTTPServer((host, port), DashboardHandler)
    except OSError as e:
        logger.error(f"Failed to bind {host}:{port} — {e}")
        return False

    _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _server_thread.start()

    logger.info(f"🌐 Web dashboard server started on http://{host}:{port}")
    return True


def stop_server():
    """Shut down the HTTP server."""
    global _server, _server_thread
    if _server:
        try:
            _server.shutdown()
        except Exception:
            pass
        _server = None
    _server_thread = None
    logger.info("Web server stopped.")