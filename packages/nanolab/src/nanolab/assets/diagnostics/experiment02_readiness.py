"""Start a platform and wait for its first correct answer; stdlib only, on the VM.

Usage:
    python3 -I experiment02_readiness.py SPEC

SPEC is a JSON file:

    {"start": [[argv...], ...], "api": URL,
     "register": {"url": URL, "check": URL, "body": {...}} | null,
     "invoke": {"url", "headers", "body", "extractor", "field", "idHeader",
                "size", "pngSha256", "timeoutSeconds"},
     "timeoutSeconds": 300, "intervalSeconds": 0.5, "out": PATH}

The clock starts immediately before the first start command, and every instant
is read here, on the VM, so no SSH round trip of the operator is part of a
timing. Each check repeats every `intervalSeconds` until it passes or the
deadline does:

1. `api` answers 2xx;
2. the function is registered: a 201 to the registration or, after any other
   answer or none, a 200 from `check`. A registration that outlived its client
   still registers, and a retry would only get a 409. A platform registered
   by its own tools gives `{"commands": [[argv...], ...]}` instead, each of
   which must succeed once, in order;
3. an invocation answers with a correct PNG, judged as the generator judges
   it. An HTTP 200 carrying an error or the wrong image is not readiness, and
   the loop goes on to the deadline.

It prints one JSON object: the instants of each step, or the stage that timed
out with the last answer seen. The first correct answer is written to `out`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXCERPT_BYTES = 512


def call(
    url: str,
    *,
    timeout: float,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int | None, bytes, dict[str, str], str | None]:
    """Return status, body, headers and error; an HTTP error status is an answer."""
    request = urllib.request.Request(
        url, data=body, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.status, answer.read(), dict(answer.headers), None
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers or {}), None
    except (OSError, ValueError) as error:
        return None, b"", {}, f"{type(error).__name__}: {error}"


def media_type(headers: object) -> str | None:
    """Return the media type of a Content-Type header, if there is one."""
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            return str(value).split(";")[0].strip().lower()
    return None


def png_of(
    invoke: dict[str, Any], status: int | None, body: bytes, headers: dict[str, str]
) -> bytes | None:
    """Return the PNG a correct answer carries, or None: the generator's rule."""
    if status != 200:
        return None
    try:
        if invoke["extractor"] == "raw-png":
            if media_type(headers) != "image/png":
                return None
            png = body
        else:
            value = json.loads(body)
            if invoke["extractor"] == "nanofaas-envelope":
                if (
                    not isinstance(value, dict)
                    or value.get("status") != "success"
                    or value.get("statusCode") != 200
                    or value.get("encoding") != "base64"
                    or media_type(value.get("headers")) != "image/png"
                ):
                    return None
                text = value.get("output")
            elif invoke.get("field") is None:
                text = value
            elif invoke["extractor"] == "json-string-base64":
                inner = value.get(invoke["field"]) if isinstance(value, dict) else None
                text = json.loads(inner) if isinstance(inner, str) else None
            else:
                if not isinstance(value, dict) or value.get("error") is not None:
                    return None
                text = value.get(invoke["field"])
            if not isinstance(text, str):
                return None
            png = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(png) < 24 or not png.startswith(PNG_SIGNATURE):
        return None
    width, height = struct.unpack(">II", png[16:24])
    if width != height or width < int(invoke["size"]):
        return None
    expected = invoke.get("pngSha256")
    if expected and hashlib.sha256(png).hexdigest() != expected:
        return None
    return png


def main(argv: list[str] | None = None) -> int:
    """Run the spec and print what happened; the outcome is in the output."""
    spec = json.loads(Path((argv or sys.argv[1:])[0]).read_text(encoding="utf-8"))
    interval = float(spec["intervalSeconds"])
    result: dict[str, Any] = {"intervalSeconds": interval}
    last: dict[str, Any] = {}
    started = time.time()
    deadline = started + float(spec["timeoutSeconds"])
    result["started"] = started

    def remaining() -> float:
        return deadline - time.time()

    def note(status: int | None, body: bytes, error: str | None) -> None:
        last.clear()
        last.update(
            status=status,
            error=error,
            body=body[:EXCERPT_BYTES].decode("utf-8", errors="replace"),
        )

    def until(stage: str, check: Any) -> bool:
        while True:
            if check():
                result[stage] = time.time()
                return True
            if remaining() <= 0:
                result["timeout"] = stage
                result["last"] = dict(last)
                return False
            time.sleep(min(interval, max(0.0, remaining())))

    for command in spec["start"]:
        done = subprocess.run(command, capture_output=True, text=True, check=False)
        if done.returncode:
            result.update(
                failed="start",
                command=command,
                code=done.returncode,
                stderr=done.stderr[-2000:],
            )
            print(json.dumps(result))
            return 0

    def api() -> bool:
        status, body, _headers, error = call(
            spec["api"], timeout=max(0.5, min(2.0, remaining()))
        )
        note(status, body, error)
        # Some platforms have no 2xx endpoint before registration: any answer
        # then means the API is up.
        return status is not None and (
            200 <= status < 300 or bool(spec.get("apiAnyStatus"))
        )

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        # A command hung on a platform that never answers ends at the deadline.
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=max(1.0, remaining()),
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(command, 124, "", "timed out")

    register = spec.get("register")

    pending = list(register.get("commands", [])) if register else []

    def run_commands() -> bool:
        # Each command must succeed once; a failure is retried, the rest wait.
        while pending:
            done = run(pending[0])
            if done.returncode:
                note(done.returncode, done.stderr.encode(), "command failed")
                return False
            pending.pop(0)
        return True

    def registered() -> bool:
        if "commands" in register:
            return run_commands()
        status, body, _headers, error = call(
            register["url"],
            method="POST",
            body=json.dumps(register["body"]).encode(),
            headers={"Content-Type": "application/json"},
            timeout=max(1.0, remaining()),
        )
        note(status, body, error)
        if status == 201:
            return True
        found, _body, _headers, _error = call(
            register["check"], timeout=max(0.5, min(5.0, remaining()))
        )
        return found == 200

    invoke = spec["invoke"]
    attempts = 0
    secret: dict[str, str] = {}

    def first() -> bool:
        nonlocal attempts
        attempts += 1
        if invoke.get("headersCommand") and not secret:
            done = run(invoke["headersCommand"])
            if done.returncode:
                note(done.returncode, b"", "headers command failed")
                return False
            secret.update(json.loads(done.stdout))
        headers = {**invoke["headers"], **secret}
        if invoke.get("idHeader"):
            headers[invoke["idHeader"]] = f"readiness-{attempts}"
        status, body, answer_headers, error = call(
            invoke["url"],
            method="POST",
            body=invoke["body"].encode(),
            headers=headers,
            timeout=max(1.0, min(float(invoke["timeoutSeconds"]), remaining())),
        )
        note(status, body, error)
        png = png_of(invoke, status, body, answer_headers)
        if png is None:
            return False
        Path(spec["out"]).write_bytes(body)
        result["pngSha256"] = hashlib.sha256(png).hexdigest()
        return True

    if until("api", api) and (register is None or until("registered", registered)):
        until("first", first)
    result["attempts"] = attempts
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
