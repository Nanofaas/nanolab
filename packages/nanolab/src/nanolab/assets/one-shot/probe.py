"""Run-owned standard-library clock and network probe for local VM validation."""
import json
import os
import platform
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAYLOAD = b"x" * (8 * 1024 * 1024)
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
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 17111), Handler).serve_forever()
