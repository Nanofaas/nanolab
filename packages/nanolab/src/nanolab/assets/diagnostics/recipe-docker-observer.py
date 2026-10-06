"""Retain recipe Docker commands and metadata before Gradle removes them.

Runs inside the soak's owned command supervisor. It creates no process group,
so cancellation reaches this process and every Docker descendant.
"""

import hashlib
import json
import os
import selectors
import subprocess
import sys
import uuid
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text())
root = Path(cfg["directory"])
requested = sys.argv[2:]
args = list(requested)
if args[:2] == ["buildx", "build"]:
    if "-f" in args and Path(args[args.index("-f") + 1]) == Path(cfg["node_source"]):
        args[args.index("-f") + 1] = cfg["node_dockerfile"]
    args += ["--progress=plain"]
name = uuid.uuid4().hex
path = root / ("docker-" + name + ".log")
limit = cfg["artifact_limit_bytes"]


def spent():
    total = 0
    pending = [cfg["budget_root"]]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.name.startswith(("workspace-", ".")):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
    return total


def descriptor(path):
    body = path.read_bytes()
    return {
        "path": path.name,
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
    }


record = {
    key: cfg[key]
    for key in ("request_id", "profile_sha256", "source_fingerprint", "workspace")
}
record.update(requested=requested, actual=args, cwd=os.getcwd())
process = None
try:
    budget = limit - spent()
    process = subprocess.Popen(
        [cfg["docker"], *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, sys.stdout.buffer)
    selector.register(process.stderr, selectors.EVENT_READ, sys.stderr.buffer)
    with path.open("xb") as log:
        written = 0
        while selector.get_map():
            for key, _ in selector.select():
                body = os.read(key.fileobj.fileno(), 65536)
                if not body:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                written += len(body)
                if written > budget:
                    raise OSError("recipe observation artifact budget exhausted")
                log.write(body)
                key.data.write(body)
                key.data.flush()
    selector.close()
    record["exit_code"] = process.wait()
    record["log"] = descriptor(path)
    if "--metadata-file" in args:
        temporary = Path(args[args.index("--metadata-file") + 1])
        if temporary.is_file():
            metadata = root / ("metadata-" + name + ".json")
            if temporary.stat().st_size > limit - spent():
                raise OSError("recipe metadata artifact budget exhausted")
            with metadata.open("xb") as output:
                output.write(temporary.read_bytes())
            record["metadata"] = descriptor(metadata)
    payload = (json.dumps(record) + "\n").encode()
    if len(payload) > limit - spent():
        raise OSError("recipe observation record budget exhausted")
    with (root / "commands.jsonl").open("ab") as output:
        output.write(payload)
    sys.exit(record["exit_code"])
finally:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
