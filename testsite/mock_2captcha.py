"""Mock 2captcha endpoint for offline E2E tests (same wire format as the real one).

  POST /in.php  -> {"status":1,"request":"<id>"}
  GET  /res.php -> action=getbalance -> {"status":1,"request":"10.00"}
                   action=get -> CAPCHA_NOT_READY on the first poll, then a token
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

STATE: dict[str, int] = {}
LOCK = threading.Lock()
READY_AFTER = int(os.environ.get("MOCK_READY_AFTER", "1"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep the fixture output quiet
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        if self.path.startswith("/in.php"):
            rid = form.get("method", ["x"])[0] + "-" + str(len(STATE) + 1)
            with LOCK:
                STATE[rid] = 0
            self._send({"status": 1, "request": rid})
        else:
            self._send({"status": 0, "request": "ERROR_UNKNOWN_METHOD"})

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        action = q.get("action", [""])[0]
        if action == "getbalance":
            self._send({"status": 1, "request": "10.00"})
            return
        rid = q.get("id", [""])[0]
        with LOCK:
            polls = STATE.get(rid, READY_AFTER)
            STATE[rid] = polls + 1
        if polls < READY_AFTER:
            self._send({"status": 0, "request": "CAPCHA_NOT_READY"})
        else:
            self._send({"status": 1, "request": f"mock-token-{rid}"})


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PORT", "8081"))
    print(f"[mock-2captcha] listening on :{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
