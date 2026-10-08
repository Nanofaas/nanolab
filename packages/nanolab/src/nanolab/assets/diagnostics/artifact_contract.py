"""Bounded private-network callback capture and probe; Python stdlib only."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import math
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=finite, parse_float=finite
    )


def create_server(settings, *, host="0.0.0.0", port=8081):
    lock = threading.Lock()
    expected, records, bodies, violations = {}, [], {}, []
    total = 0
    limit = int(settings["message_bytes"])

    def violation(message):
        if message not in violations and len(violations) < int(
            settings["capture_records"]
        ):
            violations.append(message[:512])

    class Handler(http.server.BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(float(settings["request_seconds"]))

        def log_message(self, *_):
            pass

        def reply(self, status, value, *, raw=False):
            body = value if raw else json.dumps(value, separators=(",", ":")).encode()
            if len(body) >= limit:
                with lock:
                    violation("inspection response byte bound reached")
                status, body = 413, b'{"error":"response bound"}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Type",
                "application/octet-stream" if raw else "application/json",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"ready": True})
            elif self.path == "/_nanolab/records":
                with lock:
                    value = {
                        "records": list(records),
                        "violations": list(violations),
                        "bytes": total,
                    }
                self.reply(200, value)
            elif re.fullmatch(r"/_nanolab/body/[1-9][0-9]*", self.path):
                with lock:
                    raw = bodies.get(int(self.path.rsplit("/", 1)[1]))
                self.reply(
                    200 if raw is not None else 404,
                    raw if raw is not None else b"",
                    raw=True,
                )
            else:
                self.record(b"", "unexpected route")
                self.reply(400, {"error": "unexpected route"})

        def read_body(self):
            if self.headers.get("Transfer-Encoding") is not None:
                raise ValueError("chunked request unsupported")
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0:
                raise ValueError("missing content length")
            deadline = time.monotonic() + float(settings["request_seconds"])
            remaining = min(length, limit)
            parts = []
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("request deadline")
                self.connection.settimeout(left)
                part = self.rfile.read1(min(remaining, 65536))
                if not part:
                    raise ValueError("incomplete request body")
                parts.append(part)
                remaining -= len(part)
            return b"".join(parts), length >= limit

        def record(self, raw, error=None):
            nonlocal total
            path = self.path[:512]
            matched = re.fullmatch(
                r"/v1/executions/([A-Za-z0-9_-]{1,128}):complete", self.path
            )
            execution_id = matched.group(1) if matched else None
            with lock:
                if error:
                    violation(error)
                if execution_id is None:
                    violation("unexpected route")
                elif execution_id not in expected:
                    violation(f"unknown execution: {execution_id}")
                elif expected[execution_id] == 0:
                    violation(f"unexpected callback: {execution_id}")
                elif any(record["executionId"] == execution_id for record in records):
                    violation(f"duplicate callback: {execution_id}")
                if len(records) + 1 >= int(settings["capture_records"]) or total + len(
                    raw
                ) >= int(settings["capture_bytes"]):
                    violation("capture bound reached")
                    return False
                sequence = len(records) + 1
                records.append(
                    {
                        "sequence": sequence,
                        "executionId": execution_id,
                        "method": self.command,
                        "path": path,
                        "bodyBytes": len(raw),
                        "bodySha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
                bodies[sequence] = raw
                total += len(raw)
            return error is None and execution_id is not None

        def do_POST(self):
            try:
                raw, oversized = self.read_body()
                if self.path == "/_nanolab/register":
                    value = decode(raw)
                    if (
                        oversized
                        or not isinstance(value, dict)
                        or not isinstance(value.get("executionId"), str)
                        or not re.fullmatch(
                            r"[A-Za-z0-9_-]{1,128}", value["executionId"]
                        )
                        or type(value.get("expectedCallbacks")) is not int
                        or value["expectedCallbacks"] not in (0, 1)
                    ):
                        raise ValueError("invalid registration")
                    with lock:
                        if value["executionId"] in expected or len(expected) + 1 > int(
                            settings["capture_records"]
                        ):
                            violation("registration duplicate or bound reached")
                            accepted = False
                        else:
                            expected[value["executionId"]] = value["expectedCallbacks"]
                            accepted = True
                    self.reply(200 if accepted else 400, {"registered": accepted})
                    return
                error = "request byte bound reached" if oversized else None
                if error is None:
                    try:
                        value = decode(raw)
                        if not isinstance(value, dict):
                            raise ValueError("callback must be an object")
                    except (ValueError, UnicodeError) as failure:
                        error = str(failure)
                accepted = self.record(raw[: max(0, limit - 1)], error)
                self.reply(200 if accepted else 400, {"accepted": accepted})
            except (ValueError, OSError) as failure:
                self.record(
                    b"",
                    "request deadline"
                    if isinstance(failure, (TimeoutError, socket.timeout))
                    else str(failure),
                )
                self.reply(
                    408 if isinstance(failure, (TimeoutError, socket.timeout)) else 400,
                    {"error": "invalid request"},
                )

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def probe(instruction, settings):
    """Enforce the entire request deadline, including a trickling response."""

    def expired(*_):
        raise TimeoutError("request deadline reached")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(
        signal.ITIMER_REAL, float(settings["request_seconds"])
    )
    try:
        return _probe(instruction, settings)
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def _probe(instruction, settings):
    limit = int(settings["message_bytes"])
    raw = base64.b64decode(instruction.get("bodyBase64", ""), validate=True)
    if len(raw) >= limit:
        raise ValueError("request byte bound reached")
    request = urllib.request.Request(
        instruction["url"],
        data=raw if instruction.get("method", "GET") != "GET" else None,
        method=instruction.get("method", "GET"),
        headers=instruction.get("headers", {}),
    )
    # An HTTP failure is an observation: preserve its status and body for the oracle.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        response = opener.open(request, timeout=float(settings["request_seconds"]))
    except urllib.error.HTTPError as failure:
        response = failure
    with response:
        body = response.read(limit)
        if len(body) >= limit or time.monotonic() - started >= float(
            settings["request_seconds"]
        ):
            raise ValueError("response bound or deadline reached")
        return {
            "status": response.status,
            "headers": dict(response.headers),
            "bodyBase64": base64.b64encode(body).decode(),
        }


def main():
    instruction = decode(sys.stdin.buffer.read(4 * 1024 * 1024))
    settings = instruction["settings"]
    if sys.argv[1:] == ["serve"]:
        create_server(settings).serve_forever()
    elif sys.argv[1:] == ["probe"]:
        print(json.dumps(probe(instruction, settings)))
    else:
        raise ValueError("expected serve or probe")


if __name__ == "__main__":
    main()
