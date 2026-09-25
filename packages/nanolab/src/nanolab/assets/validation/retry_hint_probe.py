#!/usr/bin/env python3
"""Deterministic retry-hint probe against an existing NanoLab control plane."""

import argparse
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

from retry_backoff_burst import request, timestamp, validate_successes


def validate_candidate(calls, attempts):
    """Check terminal envelopes and every actual upstream retry identity/gap."""
    ids = validate_successes([row["response"] for row in calls])
    groups = {execution_id: [] for execution_id in ids}
    for attempt in attempts:
        execution_id = attempt["executionId"]
        if execution_id not in groups:
            raise RuntimeError("unexpected upstream execution identity")
        groups[execution_id].append(attempt)
    for rows in groups.values():
        rows.sort(key=lambda row: row["upstreamReceivedMonotonicNanos"])
        if [row["attempt"] for row in rows] != ["1", "2"] or [
            row["outcome"] for row in rows
        ] != [429, 200]:
            raise RuntimeError("expected exactly attempts 1/refused and 2/success")
        if (
            rows[1]["upstreamReceivedMonotonicNanos"]
            - rows[0]["upstreamReceivedMonotonicNanos"]
            < 1_000_000_000
        ):
            raise RuntimeError("retry arrived before the one-second hint")


def main():
    """Record actual upstream attempts against a one-second refusal window."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--validate-candidate", action="store_true")
    args = parser.parse_args()
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=False)
    route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    route.connect((args.url.split("/")[2].split(":")[0], 8080))
    host = route.getsockname()[0]
    route.close()
    first_seen = {}
    lock = Lock()
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed = time.monotonic_ns()
            execution_id = self.headers.get("X-Execution-Id")
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with lock:
                initial = first_seen.setdefault(execution_id, observed)
            status = 429 if observed - initial < 1_000_000_000 else 200
            body = json.dumps(
                {"fixture": "retry-after-one-second", "status": status}
            ).encode()
            started = timestamp()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if status == 429:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)
            with lock:
                attempts.append(
                    {
                        "executionId": execution_id,
                        "attempt": self.headers.get("X-Dispatch-Attempt"),
                        "idempotencyKey": self.headers.get("Idempotency-Key"),
                        "upstreamReceivedAt": started,
                        "upstreamCompletedAt": timestamp(),
                        "upstreamReceivedMonotonicNanos": observed,
                        "sinceFirstAttemptNanos": observed - initial,
                        "outcome": status,
                        "retryAfter": "1" if status == 429 else None,
                    }
                )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 18090), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    function = "retry-hint-probe"
    settings = {
        "name": function,
        "image": "fixture",
        "executionMode": "EXTERNAL",
        "endpointUrl": f"http://{host}:18090/invoke",
        "timeoutMs": 5000,
        "concurrency": 8,
        "queueSize": 300,
        "maxRetries": 3,
    }
    (output / "settings.json").write_text(json.dumps(settings, indent=2))
    base = args.url.rstrip("/")
    registered = False
    try:
        status, _, body = request(base + "/v1/functions", "POST", settings)
        if status != 201:
            raise RuntimeError(f"registration failed: {status} {body}")
        registered = True

        def invoke(sequence):
            started = timestamp()
            result = request(
                f"{base}/v1/functions/{function}:invoke",
                "POST",
                {"input": {"sequence": sequence}},
                {"X-Timeout-Ms": "10000", "Idempotency-Key": f"hint-{sequence}"},
            )
            return {
                "sequence": sequence,
                "clientStartedAt": started,
                "clientCompletedAt": timestamp(),
                "response": result,
            }

        before = request(base.replace(":8080", ":8081") + "/actuator/prometheus")
        (output / "before-metrics.txt").write_text(before[2])
        with ThreadPoolExecutor(max_workers=8) as pool:
            calls = list(pool.map(invoke, range(300)))
        (output / "calls.json").write_text(json.dumps(calls, indent=2))
        (output / "attempts.json").write_text(json.dumps(attempts, indent=2))
        after = request(base.replace(":8080", ":8081") + "/actuator/prometheus")
        (output / "after-metrics.txt").write_text(after[2])
        print(
            json.dumps(
                {
                    "calls": len(calls),
                    "concurrency": 8,
                    "attempts": len(attempts),
                    "http200": sum(row["response"][0] == 200 for row in calls),
                }
            ),
            flush=True,
        )
        if args.validate_candidate:
            validate_candidate(calls, attempts)
    finally:
        if registered:
            request(f"{base}/v1/functions/{function}", "DELETE")
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
