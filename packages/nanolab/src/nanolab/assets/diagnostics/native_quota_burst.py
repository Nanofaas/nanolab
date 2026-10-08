"""Exercise the invocation quota through a bounded, concurrent HTTP burst."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Barrier
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

PAYLOAD = {"input": {"text": "alpha beta " * 25_000, "topN": 3}}
RESPONSE_LIMIT = 64 * 1024
RECEIPT_LIMIT = 4 * 1024 * 1024


def run_burst(
    url: str, function: str, payload: dict[str, object], output: Path
) -> None:
    """Retain every response and require both a success and a quota rejection."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise RuntimeError("native quota requires an HTTP endpoint")
    data = json.dumps(payload).encode()
    if len(data) >= 1024 * 1024:
        raise RuntimeError("native quota payload exceeds ingress bound")
    barrier = Barrier(30, timeout=10)

    def invoke(index: int) -> dict[str, object]:
        barrier.wait()
        request = Request(
            f"{url.rstrip('/')}/v1/functions/{quote(function, safe='')}:invoke",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            try:
                response = urlopen(request, timeout=10)  # nosec B310: scheme checked above
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(RESPONSE_LIMIT + 1)
                if len(raw) > RESPONSE_LIMIT:
                    raise RuntimeError("native quota response exceeds byte bound")
                return {"index": index, "status": response.code, "body": raw.decode()}
        except Exception as error:
            return {"index": index, "status": 0, "body": str(error)}

    responses: list[dict[str, object]] = []
    pool = ThreadPoolExecutor(max_workers=30)
    failure: Exception | None = None
    try:
        futures = [pool.submit(invoke, index) for index in range(30)]
        responses.extend(
            future.result() for future in as_completed(futures, timeout=30)
        )
    except Exception as error:
        failure = error
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        receipt = json.dumps({"function": function, "responses": responses}, indent=2)
        if len(receipt.encode()) > RECEIPT_LIMIT:
            raise RuntimeError("native quota receipt exceeds byte bound")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(receipt + "\n")
    if failure is not None or len(responses) != 30:
        raise RuntimeError("native quota burst did not finish within 30s") from failure
    accepted = rejected = 0
    for row in responses:
        try:
            body = json.loads(str(row["body"]))
        except ValueError as error:
            raise RuntimeError("native quota response is not JSON") from error
        if not isinstance(body, dict):
            raise RuntimeError("native quota response is not a JSON object")
        if row["status"] == 200:
            if (
                body.get("status") != "success"
                or body.get("statusCode") != 200
                or body.get("error")
            ):
                raise RuntimeError("native quota accepted invocation did not succeed")
            accepted += 1
        elif row["status"] == 429 and body.get("error") == "invocation_quota_exceeded":
            rejected += 1
        else:
            raise RuntimeError(
                "native quota response did not match invocation quota contract"
            )
    if not accepted or not rejected:
        raise RuntimeError(
            "native quota burst did not observe success and quota overlap"
        )


def main() -> None:
    """Run against the existing JVM word-stats function."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    run_burst(args.url, args.function, PAYLOAD, args.out)


if __name__ == "__main__":
    main()
