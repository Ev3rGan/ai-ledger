from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class AcceptanceHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if "missing" in self.path:
            status = 404
        elif "failure" in self.path:
            status = 500
        else:
            status = 200
        body = self.headers.get("X-AI-Anonymous-Client", "").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


ThreadingHTTPServer(("0.0.0.0", 8000), AcceptanceHandler).serve_forever()
