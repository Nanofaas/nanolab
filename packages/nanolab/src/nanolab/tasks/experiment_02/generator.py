"""The load generator of experiment 02: experiment 01's calendar, any platform's door.

The script is experiment 01's own, `assets/k6/experiment-01.js`. What it sends,
when, and how a request joins its cohort are therefore experiment 01's; what
changes per platform is only where the request goes, with which headers, and
where the PNG is in the answer. That is the target, serialized beside
experiment 01's protocol in `EXPERIMENT_TARGET`.

The generator runs on the loadgen VM. Its event ledger is fetched whatever k6
does, including when it fails: a cell's requests are its evidence, and a
generator that crashed halfway still sent the first half.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from sonata_tasks.k6 import k6_argv
from sonata_tasks.k6_models import K6Config

from nanolab.comparison.experiment_01 import EVENTS, SUMMARY
from nanolab.config.experiment_01 import k6_protocol
from nanolab.config.experiment_02 import Experiment02Protocol
from nanolab.tasks.experiment_02.vm import Vm

Extractor = Literal["nanofaas-envelope", "json-base64", "raw-png"]


@dataclass(frozen=True)
class K6Target:
    """Where requests go and where the PNG is in the answer.

    `nanofaas-envelope` is the control plane's invocation envelope;
    `json-base64` is a base64 PNG at `field` of a JSON object, or the whole JSON
    body when `field` is None; `raw-png` is the PNG as the response body.
    """

    url: str
    headers: Mapping[str, str]
    extractor: Extractor
    field: str | None = None
    # A header the script fills with each request's own ledger id, for targets
    # that admit one active execution per id.
    id_header: str | None = None


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_in_response(target: K6Target, body: bytes, *, size: int) -> bytes | None:
    """Return the PNG a response carries for `target`, or `None` if it is wrong.

    The same rule as the script's check: a PNG signature and a square IHDR at
    least `size` wide. Used where Python reads a response, such as readiness.
    """
    try:
        if target.extractor == "raw-png":
            png = body
        else:
            value = json.loads(body)
            if target.extractor == "nanofaas-envelope":
                if (
                    not isinstance(value, dict)
                    or value.get("status") != "success"
                    or value.get("encoding") != "base64"
                ):
                    return None
                text = value.get("output")
            elif target.field is None:
                text = value
            else:
                if not isinstance(value, dict) or value.get("error") is not None:
                    return None
                text = value.get(target.field)
            if not isinstance(text, str):
                return None
            png = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        return None
    width, height = struct.unpack(">II", png[16:24])
    if width != height or width < size:
        return None
    return png


def generator_environment(
    protocol: Experiment02Protocol,
    *,
    payload: bytes,
    body: bytes,
    target: K6Target,
) -> dict[str, str]:
    """Return the script's environment: the protocol, the exact body, the target.

    `payload` is the protocol's request; `body` is what this target is sent,
    the same request wrapped if the target needs it. The PNG size is read from
    the payload, since a wrapped body hides it.
    """
    target_values = {
        "url": target.url,
        "headers": dict(target.headers),
        "extractor": target.extractor,
        "field": target.field,
        "idHeader": target.id_header,
        "pngSha256": protocol.png_sha256,
    }
    return {
        "E01_PROTOCOL": k6_protocol(protocol, size=json.loads(payload)["size"]),
        "E01_PAYLOAD": body.decode("utf-8"),
        "EXPERIMENT_TARGET": json.dumps(
            target_values, sort_keys=True, separators=(",", ":")
        ),
    }


def run_generator(
    loadgen: Vm,
    *,
    script: Path,
    env: Mapping[str, str],
    remote_dir: PurePosixPath,
    local_dir: Path,
) -> int:
    """Run the calendar on the loadgen and fetch its ledger; return k6's exit code.

    The events are fetched even when k6 fails. The summary is fetched when k6
    wrote one; a missing summary is left missing, never replaced.
    """
    remote_script = remote_dir / script.name
    events = remote_dir / EVENTS
    summary = remote_dir / SUMMARY
    _ = loadgen.run(("mkdir", "-p", str(remote_dir)))
    _ = loadgen.run(("rm", "-f", str(events), str(summary)))
    loadgen.put(script, remote_script)
    argv = k6_argv(
        K6Config(Path(str(remote_script)), Path(str(summary)), (), dict(env))
    )
    code, _stdout, stderr = loadgen.probe(argv, env={"K6_CONSOLE_OUTPUT": str(events)})
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "k6-stderr.txt").write_text(stderr)
    _ = loadgen.get(events, local_dir / EVENTS)
    _ = loadgen.get(summary, local_dir / SUMMARY)
    return code
