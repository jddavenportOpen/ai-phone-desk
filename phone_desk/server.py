"""A minimal LoopMessage webhook receiver (stdlib only).

Binds to 127.0.0.1 by default: put it behind a tunnel or reverse proxy with TLS
and point LoopMessage's webhook URL at it. It answers 200 fast (LoopMessage
retries anything else for hours) and does no dialing itself: an approval only
queues the call, and the tick dials.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024


def serve(desk, host: str = "127.0.0.1", port: int = 8787) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, MAX_BODY)) if length else b""
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = None
            try:
                result = desk.handle_webhook(payload, {"Authorization": self.headers.get("Authorization", "")})
            except Exception as exc:  # noqa: BLE001 -- never 500 a vendor webhook into a retry storm
                result = {"ok": False, "reason": f"error: {exc}"[:200]}
            code = 401 if result.get("reason") == "unauthorized webhook" else 200
            body = json.dumps({"ok": bool(result.get("ok"))}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # quiet
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"phone-desk webhook receiver on http://{host}:{port}/ (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
