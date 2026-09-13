"""Pinned stdlib-only bridge: Docker exec remains under the existing supervisor."""

import base64
import hashlib
import json
import os
import sys


def main():
    """Relay one bounded, framed diagnostic request to the helper."""
    config = json.loads(base64.b64decode(sys.argv[1], validate=True))
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError("request exceeds helper bound")
    message = json.loads(raw)
    if message["target"] != config["config"]["target"]:
        raise ValueError("unprovisioned target")
    descriptor = os.open(config["docker"], os.O_RDONLY)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 65536):
        digest.update(chunk)
    if digest.hexdigest() != config["docker_sha256"]:
        raise ValueError("Docker executable changed")
    os.set_inheritable(descriptor, True)
    payload = base64.b64encode(
        json.dumps({"config": config["config"], "request": message}).encode()
    ).decode()
    argv = [
        config["docker"],
        "--host",
        config["docker_host"],
        "exec",
        config["helper_id"],
        "/usr/local/bin/python3",
        "/opt/nanolab/diagnostic-worker.py",
        "exchange",
        payload,
    ]
    os.execve(
        descriptor,
        argv,
        {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("DOCKER_", "LD_", "PYTHON"))
        },
    )


if __name__ == "__main__":
    main()
