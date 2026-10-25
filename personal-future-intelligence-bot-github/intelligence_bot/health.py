"""Minimal HTTP health endpoint for platforms that require a listening port."""

from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] not in {"/", "/health", "/healthz"}:
            self.send_error(404)
            return
        self._send_ok()

    def do_HEAD(self) -> None:
        if self.path.split("?", 1)[0] not in {"/", "/health", "/healthz"}:
            self.send_error(404)
            return
        self._send_ok(body=False)

    def _send_ok(self, body: bool = True) -> None:
        payload = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if body:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        # Health probes are frequent; keep them out of the news-worker logs.
        return


def start_health_server() -> ThreadingHTTPServer | None:
    """Bind the platform's PORT, if supplied, and serve a simple health check."""

    raw_port = os.environ.get("PORT")
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")

    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="health-http", daemon=True)
    thread.start()
    logging.getLogger("intelligence_bot.health").info("HTTP health endpoint listening on port %s", port)
    return server
