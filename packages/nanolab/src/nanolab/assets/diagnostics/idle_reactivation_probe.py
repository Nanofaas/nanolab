"""Remote B6 request probe; stdlib only, using the shared B5 output oracle."""

import argparse
import base64
import http.client
import json
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TIMEOUT_SECONDS = 40


def equal(actual, expected):
    """Compare JSON without accepting Python's True == 1 shortcut."""
    if type(actual) in (int, float) and type(expected) in (int, float):
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            equal(a, b) for a, b in zip(actual, expected, strict=True)
        )
    return actual == expected


def load_inputs(corpus_path, contract_path):
    """Require four unchanged medium inputs and their generated exact oracle."""
    corpus = json.loads(Path(corpus_path).read_text())
    contract = json.loads(Path(contract_path).read_text())
    if (corpus.get("profile") != "medium" or len(corpus.get("cases", [])) != 4
            or contract.get("contract") != "word-stats-runtime-v1"):
        raise ValueError("B6 requires the B5 medium corpus and runtime oracle v1")
    fixtures = []
    for case in corpus["cases"]:
        matches = [c for c in contract["cases"] if c["name"] == "benchmark " + case["name"]]
        if (len(matches) != 1 or "expected" not in matches[0]
                or not equal(matches[0]["input"], case["input"])):
            raise ValueError("Missing or stale oracle: " + case["name"])
        fixtures.append(matches[0])
    return fixtures


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Record the original HTTP response rather than following a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def invoke(url, function, fixture, request_id=None):
    """Measure one full response on this machine with no client retry."""
    request_id = request_id or uuid.uuid4().hex
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/functions/" + urllib.parse.quote(function, safe="") + ":invoke",
        data=json.dumps({"input": fixture["input"]}).encode(),
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        method="POST",
    )
    # Each observation uses a new connection in both arms, including warm calls.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    status, body, error = 0, b"", None
    wall_start, start = time.time(), time.monotonic_ns()
    def expired(signum, frame):
        raise TimeoutError("whole-response deadline exceeded")

    old_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, TIMEOUT_SECONDS)
    try:
        try:
            response = client.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as http_error:
            response = http_error
        with response:
            status = response.status
            body = response.read()
    except (OSError, urllib.error.URLError, http.client.HTTPException) as failure:
        error = str(failure)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    end = time.monotonic_ns()
    valid = False
    if status == 200 and error is None:
        try:
            decoded = json.loads(body)
            valid = (isinstance(decoded, dict) and decoded.get("status") == "success"
                     and equal(decoded.get("output"), fixture["expected"]))
        except (ValueError, UnicodeError):
            pass
    return {"request_id": request_id, "started_at": wall_start,
            "started_monotonic_ns": start, "ended_monotonic_ns": end,
            "duration_ms": (end - start) / 1_000_000, "status": status,
            "valid": valid, "error": error, "body_base64": base64.b64encode(body).decode()}


def main():
    """Write each attempt durably before returning the burst to its workflow."""
    parser = argparse.ArgumentParser()
    for option in ("url", "function", "corpus", "contract", "output"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--phase", choices=("qualification", "warmup", "cycle"), required=True)
    parser.add_argument("--cycle", type=int, default=0)
    args = parser.parse_args()
    fixtures = load_inputs(args.corpus, args.contract)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an interrupted attempt when a command is accidentally rerun.
    with output.open("x") as stream:
        def emit(record):
            line = json.dumps(record)
            stream.write(line + "\n")
            stream.flush()
            print(line, flush=True)

        count = {"qualification": 4, "warmup": 60, "cycle": 6}[args.phase]
        for index in range(count):
            fixture = fixtures[(args.cycle if args.phase == "cycle" else index) % 4]
            identity = dict(phase=args.phase, cycle=args.cycle, part=index,
                            case_name=fixture["name"], function=args.function,
                            request_id=uuid.uuid4().hex)
            emit(dict(identity, event="started", time=time.time()))
            record = invoke(args.url, args.function, fixture, identity["request_id"])
            record.update(identity, event="response")
            emit(record)
            # Any failed attempt leaves no trustworthy next idle boundary.
            if not record["valid"]:
                break
            if args.phase == "warmup":
                time.sleep(max(0, 1 - record["duration_ms"] / 1000))


if __name__ == "__main__":
    main()
