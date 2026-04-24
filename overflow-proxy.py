"""Tiny SSE rewriting proxy for litellm Anthropic passthrough.

Sits between Claude Code and litellm, rewriting the model name in
message_start events so Claude Code sees the model name it requested
rather than the upstream provider's name (e.g., minimax/minimax-m2.7).

Usage: python overflow-proxy.py --port 4001 --upstream http://localhost:4000
"""

import http.client
import json
import socketserver
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

UPSTREAM = "http://localhost:4000"
PORT = 4001


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Forward GET requests (e.g., health checks) to upstream."""
        parsed = urlparse(UPSTREAM)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
        headers = {}
        for h in self.headers:
            headers[h] = self.headers[h]
        try:
            conn.request("GET", self.path, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")
        finally:
            conn.close()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)

        # Extract requested model from the request body
        requested_model = None
        try:
            req_json = json.loads(body)
            requested_model = req_json.get("model")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        # Forward to litellm using http.client for streaming support
        parsed = urlparse(UPSTREAM)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=300)
        headers = {}
        for header in ("content-type", "x-api-key", "authorization",
                       "anthropic-version", "anthropic-beta"):
            val = self.headers.get(header)
            if val:
                headers[header] = val

        try:
            conn.request("POST", self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")
            return

        # Send response headers
        self.send_response(resp.status)
        ct = resp.getheader("Content-Type", "")
        for key in ("content-type", "x-litellm-call-id", "x-litellm-version"):
            val = resp.getheader(key)
            if val:
                self.send_header(key, val)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        if "event-stream" in ct and requested_model:
            self._stream_rewrite(resp, requested_model)
        else:
            data = resp.read()
            if requested_model:
                try:
                    d = json.loads(data)
                    if "model" in d:
                        d["model"] = requested_model
                    data = json.dumps(d).encode()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            self._send_chunk(data)
        self._send_chunk(b"")  # End chunked encoding
        conn.close()

    def _send_chunk(self, data):
        """Send a chunk in HTTP chunked transfer encoding."""
        self.wfile.write(f"{len(data):x}\r\n".encode())
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream_rewrite(self, resp, requested_model):
        """Stream SSE events, rewriting model name in message_start."""
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                if buf:
                    self._send_chunk(buf)
                break
            buf += chunk
            while b"\n\n" in buf:
                event_data, buf = buf.split(b"\n\n", 1)
                event_str = event_data.decode("utf-8", errors="replace")
                # Rewrite model in message_start
                if '"message_start"' in event_str and '"model"' in event_str:
                    lines = event_str.split("\n")
                    for i, line in enumerate(lines):
                        if line.startswith("data: "):
                            try:
                                d = json.loads(line[6:])
                                msg = d.get("message", {})
                                if "model" in msg:
                                    msg["model"] = requested_model
                                lines[i] = "data: " + json.dumps(d)
                            except (json.JSONDecodeError, KeyError):
                                pass
                    event_str = "\n".join(lines)
                self._send_chunk((event_str + "\n\n").encode())

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[proxy] {fmt % args}\n")
        sys.stderr.flush()


def main():
    port = PORT
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        elif arg == "--upstream" and i + 1 < len(sys.argv):
            global UPSTREAM
            UPSTREAM = sys.argv[i + 1]

    server = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"overflow-proxy listening on 127.0.0.1:{port} -> {UPSTREAM}",
          file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
