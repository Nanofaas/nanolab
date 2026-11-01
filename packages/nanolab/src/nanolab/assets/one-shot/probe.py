"""Run-owned standard-library clock and network probe for local VM validation."""
import http.client
import json
import os
import platform
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAYLOAD = b"x" * (8 * 1024 * 1024)
RELAYED_HEADERS = ("Content-Type", "X-Execution-Id", "X-Dispatch-Attempt")
with open("/proc/sys/kernel/random/boot_id") as boot_identity:
    BOOT_ID = boot_identity.read().strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Separate header/body writes must not add delayed-ACK latency to clock probes.
    disable_nagle_algorithm = True
    def do_GET(self):
        if self.path == "/clock":
            data = json.dumps({"timestamp": time.time(),
                "monotonicSeconds": time.monotonic(), "bootId": BOOT_ID}).encode()
        elif self.path == "/inventory":
            data = json.dumps({"cpus": os.cpu_count(), "architecture": platform.machine(),
                "os": platform.platform(), "memoryMiB": int(open("/proc/meminfo").read().split()[1]) // 1024,
                "hostname": platform.node()}).encode()
        elif self.path == "/bytes":
            data = PAYLOAD
        elif self.path.startswith("/relay/"):
            self.relay()
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.startswith("/relay/"):
            self.relay()
        else:
            self.send_error(404)

    def relay(self):
        # Replicas publish dynamic ports that only the VM can reach when it is
        # behind NAT. Relay to loopback only the runtime's read-only endpoints,
        # its metrics, and the direct invocation the preflight warms replicas with.
        _, _, port, rest = self.path.split("/", 3)
        if self.command == "POST":
            allowed = rest == "invoke"
        else:
            allowed = rest.startswith("runtime/") or rest == "metrics"
        if not port.isdigit() or not 0 < int(port) < 65536 or not allowed:
            self.send_error(403)
            return
        body, headers = None, {}
        if self.command == "POST":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            headers = {key: self.headers[key] for key in RELAYED_HEADERS if key in self.headers}
        try:
            # An invocation lasts as long as its handler: the caller bounds it.
            connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=600)
            connection.request(self.command, "/" + rest, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            status, kind = response.status, response.getheader("Content-Type")
            connection.close()
        except OSError:
            self.send_error(502)
            return
        self.send_response(status)
        if kind:
            self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 17111), Handler).serve_forever()
