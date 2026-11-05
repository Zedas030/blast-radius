"""Minimal fake Home Assistant for the end-to-end test (stdlib only).

GET  /api/states                      -> current states
POST /api/services/<domain>/<svc>     -> recorded as notification
POST /api/states/<entity_id>          -> recorded as published state
POST /_control/states  [..]           -> replace the states list
GET  /_control/log                    -> {"notifications": [...], "published": [...], "auth": [...]}
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"states": [], "notifications": [], "published": [], "auth": []}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body):  # type: ignore[no-untyped-def]
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):  # type: ignore[no-untyped-def]
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null")

    def do_GET(self):  # noqa: N802
        if self.path == "/api/states":
            STATE["auth"].append(self.headers.get("Authorization", ""))
            return self._send(200, STATE["states"])
        if self.path == "/_control/log":
            return self._send(200, {k: STATE[k] for k in ("notifications", "published", "auth")})
        return self._send(404, {})

    def do_POST(self):  # noqa: N802
        body = self._body()
        if self.path == "/_control/states":
            STATE["states"] = body
            return self._send(200, {"ok": True})
        if self.path.startswith("/api/services/"):
            STATE["notifications"].append({"service": self.path.split("/api/services/")[1], **(body or {})})
            return self._send(200, [])
        if self.path.startswith("/api/states/"):
            STATE["published"].append({"entity_id": self.path.split("/api/states/")[1], **(body or {})})
            STATE["published"] = STATE["published"][-20:]
            return self._send(200, {})
        return self._send(404, {})

    def log_message(self, *args):  # type: ignore[no-untyped-def]
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8123), H).serve_forever()  # noqa: S104
