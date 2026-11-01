"""A tiny local HTTP server for channel tests.

Routes map (method, path prefix) to (status, body[, headers]). A dict body is
sent as JSON, bytes as-is; the response DROP closes the connection unanswered.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote


DROP = object()


class FakeHttpApi:
    def __init__(self):
        self.requests = []  # (method, unquoted path, headers, json body or None)
        self.raw_paths = []  # paths exactly as sent on the request line
        self.routes = {}  # (method, path prefix) -> (status, body)
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                api.raw_paths.append(self.path)
                path = unquote(self.path)
                api.requests.append(
                    (self.command, path, dict(self.headers), json.loads(raw) if raw else None)
                )
                response = api.route(self.command, path)
                if response is DROP:
                    self.close_connection = True
                    return
                status, body, *rest = response
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in (rest[0] if rest else {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def route(self, method, path):
        for (m, prefix), response in self.routes.items():
            if m == method and path.startswith(prefix):
                return response
        return 404, {"errcode": "M_UNRECOGNIZED"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
