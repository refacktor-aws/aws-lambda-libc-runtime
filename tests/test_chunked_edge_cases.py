"""Edge-case tests for PR#5's chunked transfer-encoding parser.

Round-trip tests verify data integrity through the full pipeline:
  mock server -> chunked HTTP -> C runtime parser -> handler echo -> POST back

Crash tests verify the runtime fails cleanly (exit 255, not hang/segfault)
on malformed chunked responses.

References review issues by number (see PR#5 review).
"""

import json
import subprocess
import sys
import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

BOOTSTRAP_PATH = os.environ.get("BOOTSTRAP_PATH", "/build/target/bootstrap")

PREAMBLE = b'{"message":"Hello from C! Event received: '
SUFFIX = b'"}'


def _encode_chunks(chunks: list[bytes]) -> bytes:
    """Encode a list of byte chunks into HTTP chunked transfer-encoding."""
    parts = []
    for chunk in chunks:
        parts.append(b"%x\r\n%b\r\n" % (len(chunk), chunk))
    parts.append(b"0\r\n\r\n")
    return b"".join(parts)


def _make_roundtrip_handler(chunked_wire_bytes: bytes, te_header: bytes = b"Transfer-Encoding: chunked\r\n"):
    """Handler that sends pre-built chunked response and captures the echo."""

    class Handler(BaseHTTPRequestHandler):
        count = 0
        post_body = None

        def do_GET(self):
            cls = type(self)
            if self.path.endswith("/invocation/next") and cls.count == 0:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                self.wfile.write(b"Content-Type: application/json\r\n")
                self.wfile.write(b"Lambda-Runtime-Aws-Request-Id: edge-test\r\n")
                self.wfile.write(te_header)
                self.wfile.write(b"\r\n")
                self.wfile.write(chunked_wire_bytes)
                self.wfile.flush()
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
                cls = type(self)
                cls.post_body = self.rfile.read(content_len)

        def log_message(self, format, *args):
            pass

    return Handler


def _make_crash_handler(raw_response: bytes):
    """Handler that writes a raw (possibly malformed) HTTP response."""

    class Handler(BaseHTTPRequestHandler):
        count = 0

        def do_GET(self):
            cls = type(self)
            if self.path.endswith("/invocation/next") and cls.count == 0:
                self.wfile.write(raw_response)
                self.wfile.flush()
                self.connection.close()
                cls.count += 1
            else:
                self.send_response(410)
                self.end_headers()

        def do_POST(self):
            if self.path.endswith("/response"):
                self.send_response(200)
                self.send_header("Content-Length", 0)
                self.end_headers()
                self.rfile.read(int(self.headers["Content-Length"]))

        def log_message(self, format, *args):
            pass

    return Handler


def _run_roundtrip(handler_cls, expected_body: bytes):
    """Run bootstrap against handler, verify echoed body matches expected."""
    handler_cls.count = 0
    handler_cls.post_body = None

    server = HTTPServer(("0.0.0.0", 0), handler_cls)
    port = server.server_address[1]
    t = Thread(target=server.serve_forever)
    t.daemon = True
    t.start()

    try:
        result = subprocess.run(
            [BOOTSTRAP_PATH], capture_output=True, check=True,
            env={"AWS_LAMBDA_RUNTIME_API": f"localhost:{port}"})
    finally:
        server.shutdown()

    # Verify handler received the POST
    assert handler_cls.post_body is not None, "Runtime never POSTed a response"

    # The C handler wraps the body: {"message":"Hello from C! Event received: <escaped>"}
    # Extract the echoed portion and unescape \"
    post = handler_cls.post_body
    assert post.startswith(PREAMBLE), f"Unexpected prefix: {post[:60]}"
    assert post.endswith(SUFFIX), f"Unexpected suffix: {post[-20:]}"
    echoed = post[len(PREAMBLE):-len(SUFFIX)].replace(b'\\"', b'"')

    assert echoed == expected_body, (
        f"Round-trip mismatch: sent {len(expected_body)} bytes, "
        f"got {len(echoed)} bytes back.\n"
        f"First diff at byte {next(i for i,(a,b) in enumerate(zip(expected_body, echoed)) if a!=b) if len(expected_body)==len(echoed) else min(len(expected_body),len(echoed))}"
    )


def _run_expect_crash(handler_cls):
    """Run bootstrap against handler, expect non-zero exit (FATAL)."""
    handler_cls.count = 0

    server = HTTPServer(("0.0.0.0", 0), handler_cls)
    port = server.server_address[1]
    t = Thread(target=server.serve_forever)
    t.daemon = True
    t.start()

    try:
        result = subprocess.run(
            [BOOTSTRAP_PATH], capture_output=True, timeout=5,
            env={"AWS_LAMBDA_RUNTIME_API": f"localhost:{port}"})
    except subprocess.TimeoutExpired:
        raise AssertionError("Runtime hung instead of crashing")
    finally:
        server.shutdown()

    assert result.returncode != 0, (
        f"Expected crash but runtime exited {result.returncode}\n"
        f"stderr: {result.stderr[-200:]}"
    )
    return result


# ---------------------------------------------------------------------------
# Round-trip tests (data integrity)
# ---------------------------------------------------------------------------

def test_many_tiny_chunks():
    """Stress memmove compaction: 1000 x 1-byte chunks, verify round-trip."""
    body = (b"ABCDEFGHIJ" * 100)  # 1000 bytes
    chunks = [bytes([b]) for b in body]  # 1000 x 1-byte chunks
    wire = _encode_chunks(chunks)

    handler = _make_roundtrip_handler(wire)
    print(f"Tiny chunks: {len(body)} bytes in {len(chunks)} chunks", file=sys.stderr)
    _run_roundtrip(handler, body)


def test_empty_chunked_body():
    """Edge case: chunked response with just terminal chunk (empty payload)."""
    wire = b"0\r\n\r\n"  # terminal chunk only

    handler = _make_roundtrip_handler(wire)
    print("Empty chunked body", file=sys.stderr)
    _run_roundtrip(handler, b"")


def test_chunks_containing_crlf():
    """Chunk data containing \\r\\n and fake terminal-chunk bytes.

    If the parser incorrectly scans WITHIN chunk data instead of jumping
    over it (parse_point = chunk_end + 2), it would find the embedded
    0\\r\\n and treat it as the terminal chunk, truncating the body.
    """
    # Craft body with bytes that look like chunked framing
    part1 = b"hello\r\n0\r\n\r\nfake-end"  # contains fake terminal chunk
    part2 = b"\r\nmore\r\ndata\r\n"
    body = part1 + part2

    chunks = [part1, part2]
    wire = _encode_chunks(chunks)

    handler = _make_roundtrip_handler(wire)
    print(f"CRLF in chunks: {len(body)} bytes in {len(chunks)} chunks", file=sys.stderr)
    _run_roundtrip(handler, body)


def test_uneven_chunk_sizes():
    """Chunks of wildly varying sizes: 1, 4096, 3, 8192, 7 bytes."""
    body = b""
    chunk_sizes = [1, 4096, 3, 8192, 7]
    chunks = []
    for size in chunk_sizes:
        chunk = bytes([(i % 26) + ord('A') for i in range(size)])
        chunks.append(chunk)
        body += chunk

    wire = _encode_chunks(chunks)

    handler = _make_roundtrip_handler(wire)
    print(f"Uneven chunks: {len(body)} bytes in {len(chunks)} chunks {chunk_sizes}", file=sys.stderr)
    _run_roundtrip(handler, body)


# ---------------------------------------------------------------------------
# Crash tests (clean failure on malformed input)
# ---------------------------------------------------------------------------

def test_oversized_chunk_size():
    """Review issue #4: chunk_size > buffer should FATAL("Buffer overflow").

    Sends hex FFFFFFFF (4 GB) as chunk size. The parser sets parse_point
    past the buffer, and the overflow check on the next iteration catches it.
    """
    raw = (
        b"HTTP/1.1 200 OK\r\n"
        b"Lambda-Runtime-Aws-Request-Id: test\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"FFFFFFFF\r\n"
    )
    handler = _make_crash_handler(raw)
    result = _run_expect_crash(handler)
    assert b"Buffer overflow" in result.stderr, f"Expected 'Buffer overflow' in: {result.stderr[-200:]}"


def test_missing_terminal_chunk():
    """Review issue #5: valid chunk + connection close, no terminal 0\\r\\n\\r\\n.

    The runtime tries to recv more data, gets 0 (closed), hits FATAL.
    """
    body = b'{"msg":"hello"}'
    raw = (
        b"HTTP/1.1 200 OK\r\n"
        b"Lambda-Runtime-Aws-Request-Id: test\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        + b"%x\r\n%b\r\n" % (len(body), body)
        # no terminal chunk — connection closes
    )
    handler = _make_crash_handler(raw)
    result = _run_expect_crash(handler)
    assert b"Failed to receive bytes" in result.stderr, f"stderr: {result.stderr[-200:]}"


def test_chunked_header_trailing_space():
    """Review issue #1: 'Transfer-Encoding: chunked ' (trailing space).

    HTTP allows optional whitespace around header values (OWS in RFC 7230).
    The runtime should still detect chunked encoding and round-trip correctly.
    """
    body = b'{"msg":"trailing-space-test"}'
    wire = _encode_chunks([body])

    handler = _make_roundtrip_handler(wire, te_header=b"Transfer-Encoding: chunked \r\n")
    _run_roundtrip(handler, body)


def test_chunked_header_lowercase():
    """Review issue #1: 'transfer-encoding: chunked' (lowercase).

    HTTP headers are case-insensitive per RFC 7230. The runtime should
    detect chunked encoding regardless of header name casing.
    """
    body = b'{"msg":"lowercase-test"}'
    wire = _encode_chunks([body])

    handler = _make_roundtrip_handler(wire, te_header=b"transfer-encoding: chunked\r\n")
    _run_roundtrip(handler, body)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        BOOTSTRAP_PATH = sys.argv[1]
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{'='*60}\n{name}\n{'='*60}", file=sys.stderr)
            try:
                fn()
                print(f"  PASSED", file=sys.stderr)
            except Exception as e:
                print(f"  FAILED: {e}", file=sys.stderr)
