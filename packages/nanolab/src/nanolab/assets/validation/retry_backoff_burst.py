#!/usr/bin/env python3
"""Run inside an existing NanoLab k8s VM; this script provisions nothing."""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def timestamp():
    """Return an explicitly zoned client observation timestamp."""
    return datetime.now(UTC).isoformat()


def request(url, method="GET", body=None, headers=None):
    """Retain HTTP failures as evidence rather than abort the burst."""
    data = None if body is None else json.dumps(body).encode()
    req = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urlopen(req, timeout=25) as response:
            return response.status, dict(response.headers), response.read().decode()
    except HTTPError as error:
        return error.code, dict(error.headers), error.read().decode()
    except (URLError, TimeoutError) as error:
        return 0, {}, str(error)


def validate_successes(responses):
    """Require the candidate's 300 distinct admissions to finish successfully."""
    ids = []
    for status, _, body in responses:
        try:
            envelope = json.loads(body)
        except (ValueError, TypeError) as error:
            raise RuntimeError("invalid invocation envelope") from error
        if (
            status != 200
            or not isinstance(envelope, dict)
            or envelope.get("status") != "success"
            or envelope.get("error")
            or not envelope.get("executionId")
        ):
            raise RuntimeError("candidate invocation did not succeed")
        ids.append(envelope["executionId"])
    if len(ids) != 300 or len(set(ids)) != 300:
        raise RuntimeError("expected 300 distinct successful admissions")
    return ids


def main():
    """Register one probe, run the three phases, and delete it."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--namespace", default="nanofaas-e2e")
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--validate-candidate", action="store_true")
    args = parser.parse_args()
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=False)
    name = "retry-backoff-probe"
    base = args.url.rstrip("/")
    function_url = f"{base}/v1/functions/{name}"

    def kubectl(*argv):
        result = subprocess.run(
            ["kubectl", "-n", args.namespace, *argv],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    def snapshot(label):
        for resource in ("pods", "deployments", "services", "endpointslices"):
            (output / f"{label}-{resource}.json").write_text(
                kubectl("get", resource, "-o", "json")
            )
        pods = json.loads(kubectl("get", "pods", "-o", "json"))["items"]
        for pod in pods:
            pod_name = pod["metadata"]["name"]
            if "control-plane" in pod_name or name in pod_name:
                try:
                    logs = kubectl("logs", pod_name, "--timestamps=true")
                except RuntimeError as error:
                    logs = "Log unavailable at snapshot: " + str(error)
                (output / f"{label}-{pod_name}.log").write_text(logs)
        status, _, body = request(
            base.replace(":8080", ":8081") + "/actuator/prometheus"
        )
        (output / f"{label}-metrics.txt").write_text(f"# HTTP {status}\n{body}")

    def invoke(sequence, phase):
        started = timestamp()
        status, headers, body = request(
            function_url + ":invoke",
            "POST",
            {
                "input": {
                    "text": f"alpha beta alpha phase {phase} call {sequence}",
                    "topN": 3,
                }
            },
            {"X-Timeout-Ms": "10000", "Idempotency-Key": f"{phase}-{sequence}"},
        )
        completed = timestamp()
        execution_id = next(
            (v for k, v in headers.items() if k.lower() == "x-execution-id"), None
        )
        if execution_id is None:
            with suppress(json.JSONDecodeError, AttributeError):
                execution_id = json.loads(body).get("executionId")
        return {
            "phase": phase,
            "sequence": sequence,
            "clientStartedAt": started,
            "clientCompletedAt": completed,
            "httpStatus": status,
            "executionId": execution_id,
            "headers": headers,
            "body": body,
            "executionSnapshot": None,
        }

    phases = []

    def burst(phase):
        # Exactly 300 calls; eight client workers throughout each phase.
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda i: invoke(i, phase), range(300)))
        for row in rows:
            if row["executionId"]:
                row["executionSnapshot"] = request(
                    f"{base}/v1/executions/{row['executionId']}"
                )
        (output / f"{phase}-calls.json").write_text(json.dumps(rows, indent=2) + "\n")
        phases.append(rows)
        snapshot(phase + "-after")
        print(
            json.dumps(
                {
                    "phase": phase,
                    "calls": len(rows),
                    "concurrency": 8,
                    "http200": sum(row["httpStatus"] == 200 for row in rows),
                }
            ),
            flush=True,
        )

    stopped = Event()
    ready = Event()
    readiness_error = []

    def observe_readiness():
        try:
            with (output / "readiness.jsonl").open("w") as stream:
                while not stopped.is_set():
                    slices = json.loads(kubectl("get", "endpointslices", "-o", "json"))
                    selected = [
                        item
                        for item in slices["items"]
                        if name
                        in item["metadata"]["labels"].get(
                            "kubernetes.io/service-name", ""
                        )
                    ]
                    stream.write(
                        json.dumps({"observedAt": timestamp(), "slices": selected})
                        + "\n"
                    )
                    stream.flush()
                    if any(
                        endpoint.get("conditions", {}).get("ready") is True
                        for item in selected
                        for endpoint in (item.get("endpoints") or [])
                    ):
                        ready.set()
                    stopped.wait(0.2)
        except Exception as error:
            readiness_error.append(str(error))

    settings = {
        "name": name,
        "image": args.image,
        "executionMode": "DEPLOYMENT",
        "timeoutMs": 5000,
        "concurrency": 8,
        "queueSize": 300,
        "maxRetries": 3,
    }
    (output / "settings.json").write_text(
        json.dumps(
            {
                "function": settings,
                "waiterTimeoutMs": 10000,
                "clientConcurrency": 8,
                "callsPerPhase": 300,
            },
            indent=2,
        )
    )
    snapshot("before")
    registered = False
    watcher = Thread(target=observe_readiness, daemon=True)
    try:
        response = request(base + "/v1/functions", "POST", settings)
        (output / "registration.json").write_text(
            json.dumps({"observedAt": timestamp(), "response": response})
        )
        if response[0] != 201:
            raise RuntimeError(f"registration failed: {response}")
        registered = True
        watcher.start()
        burst("immediate")
        if not ready.wait(120):
            raise RuntimeError(f"no ready endpoints observed: {readiness_error}")
        snapshot("ready-before")
        burst("ready")
        snapshot("warm-before")
        burst("warm")
        if args.validate_candidate:
            ids = []
            for rows in phases:
                ids.extend(
                    validate_successes(
                        [
                            (row["httpStatus"], row["headers"], row["body"])
                            for row in rows
                        ]
                    )
                )
            if len(set(ids)) != 900:
                raise RuntimeError("execution identity reused across phases")
    finally:
        stopped.set()
        if watcher.is_alive():
            watcher.join(35)
        if registered:
            (output / "deletion.json").write_text(
                json.dumps(request(function_url, "DELETE"))
            )
    if readiness_error:
        raise RuntimeError(f"readiness observation failed: {readiness_error}")


if __name__ == "__main__":
    main()
