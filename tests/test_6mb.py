"""Stress test: 6 MB chunked payload at the Lambda max request size.

Two test cases:
  - test_6mb_single_chunk: one ~6 MB chunk, matching the actual AL2023
    Runtime API behaviour observed via count_chunks.py (2026-03-01).
  - test_6mb_multi_chunk: 384 x 16 KB chunks, defensive test in case
    AWS changes the Runtime API to stream in smaller pieces.
"""

import json
import subprocess
import sys
import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

BOOTSTRAP_PATH = os.environ.get("BOOTSTRAP_PATH", "/build/target/bootstrap")

BODY_SIZE = 6 * 1024 * 1024 - 256  # 6,291,200 B — leaves room for echo wrapper in response buffer
MULTI_CHUNK_SIZE = 16384  # 16 KB

# Build the event body once at import time
_marker = "6MB_STRESS_TEST"
_padding_len = BODY_SIZE - len(json.dumps({"marker": _marker, "p": ""}))
test_event_body = json.dumps({"marker": _marker, "p": "X" * _padding_len}).encode("utf-8")


def encode_single_chunk(data: bytes) -> bytes:
    """Encode data as one HTTP chunk (matching AL2023 behaviour) plus terminal chunk."""
    return b"%x\r\n%b\r\n0\r\n\r\n" % (len(data), data)


def encode_multi_chunk(data: bytes, chunk_size: int) -> bytes:
    """Encode data as multiple HTTP chunks of chunk_size bytes."""
    parts = []
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + chunk_size]
        parts.append(b"%x\r\n%b\r\n" % (len(chunk), chunk))
        offset += chunk_size
    parts.append(b"0\r\n\r\n")
    return b"".join(parts)


def _make_handler(encode_fn, port_label):
    """Create a handler class that uses the given chunked encoding function."""

    class Handler(BaseHTTPRequestHandler):
        count = 0

        def do_GET(self):
            cls = type(self)
            print(f"[{port_label}] GET {self.path} count={cls.count}", file=sys.stderr)

            if self.path.endswith("/2018-06-01/runtime/invocation/next") and cls.count == 0:
                chunked_body = encode_fn(test_event_body)

                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                self.wfile.write(b"Content-Type: application/json\r\n")
                self.wfile.write(b"Lambda-Runtime-Aws-Request-Id: test-6mb-request\r\n")
                self.wfile.write(b"Transfer-Encoding: chunked\r\n")
                self.wfile.write(b"\r\n")
                self.wfile.write(chunked_body)
                self.wfile.flush()

                print(f"[{port_label}] Sent {len(test_event_body)} bytes", file=sys.stderr)
                cls.count += 1

            else:
                self.send_response(410)
                self.end_headers()
                sys.exit(0)

        def do_POST(self):
            if self.path.endswith("/response"):
                self.send_response(200)
                self.send_header("Content-Length", 0)
                self.end_headers()
                content_len = int(self.headers["Content-Length"])
                response_bytes = self.rfile.read(content_len)
                assert content_len > BODY_SIZE, f"Response too small: {content_len}"
                assert _marker.encode() in response_bytes, "Marker not found in response"
                print(f"[{port_label}] POST response OK: {content_len} bytes", file=sys.stderr)

        def log_message(self, format, *args):
            pass

    return Handler


def _run_test(handler_cls, port):
    handler_cls.count = 0
    mock_server = HTTPServer(("0.0.0.0", port), handler_cls)
    t = Thread(target=mock_server.serve_forever)
    t.daemon = True
    t.start()

    try:
        subprocess.run(
            [BOOTSTRAP_PATH], capture_output=False, check=True,
            env={"AWS_LAMBDA_RUNTIME_API": f"localhost:{port}"})
    finally:
        mock_server.shutdown()


def test_6mb_single_chunk():
    """Matches observed AL2023 behaviour: entire payload in one chunk."""
    assert os.path.exists(BOOTSTRAP_PATH), f"Bootstrap not found: {BOOTSTRAP_PATH}"
    handler = _make_handler(encode_single_chunk, "single")
    print(f"6 MB single-chunk test: {len(test_event_body)} bytes", file=sys.stderr)
    _run_test(handler, 8082)


def test_6mb_multi_chunk():
    """Defensive: 384 x 16 KB chunks in case AWS changes chunking strategy."""
    assert os.path.exists(BOOTSTRAP_PATH), f"Bootstrap not found: {BOOTSTRAP_PATH}"
    encoder = lambda data: encode_multi_chunk(data, MULTI_CHUNK_SIZE)
    handler = _make_handler(encoder, "multi")
    n_chunks = (len(test_event_body) + MULTI_CHUNK_SIZE - 1) // MULTI_CHUNK_SIZE
    print(f"6 MB multi-chunk test: {len(test_event_body)} bytes, {n_chunks} x {MULTI_CHUNK_SIZE} B",
          file=sys.stderr)
    _run_test(handler, 8083)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        BOOTSTRAP_PATH = sys.argv[1]
    test_6mb_single_chunk()
    test_6mb_multi_chunk()
