import json
import subprocess
import sys
import os
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

BOOTSTRAP_PATH = os.environ.get("BOOTSTRAP_PATH", "/build/target/bootstrap")

test_event = {"message": "Test event"}
count = 0


class MockServerRequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        global count
        print(f"GET request path: {self.path}, count={count}", file=sys.stderr)

        if self.path.endswith("/2018-06-01/runtime/invocation/next") and count == 0:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Lambda-Runtime-Aws-Request-Id", "test-request-id")
            self.send_header("Content-Length", len(json.dumps(test_event)))
            self.end_headers()
            self.wfile.write(json.dumps(test_event).encode("utf-8"))
            print("Returned test event", file=sys.stderr)
            count += 1

        else:
            self.send_response(410)
            self.end_headers()
            sys.exit(0)

    def do_POST(self):
        if self.path.endswith("/response"):
            self.send_response(200)
            self.send_header("Content-Length", 0)
            self.end_headers()
            response_jstr = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
            response_data = json.loads(response_jstr)
            assert "message" in response_data
            assert test_event["message"] in response_data["message"], "Response: " + str(response_data)

    def log_message(self, format, *args):
        pass


def test_basic():
    global count
    count = 0

    assert os.path.exists(BOOTSTRAP_PATH), f"Bootstrap not found: {BOOTSTRAP_PATH}"

    mock_server = HTTPServer(("0.0.0.0", 8080), MockServerRequestHandler)
    t = Thread(target=mock_server.serve_forever)
    t.daemon = True
    t.start()

    print("Mock server thread started", file=sys.stderr)

    try:
        result = subprocess.run(
            [BOOTSTRAP_PATH], capture_output=False, check=True,
            env={"AWS_LAMBDA_RUNTIME_API": "localhost:8080"})
    finally:
        mock_server.shutdown()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        BOOTSTRAP_PATH = sys.argv[1]
    test_basic()
