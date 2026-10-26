"""The load generator of experiment 02: experiment 01's calendar, any platform's door.

The script is `assets/k6/experiment-02.js`. What it sends, when, and how a
request joins its cohort are experiment 01's; what changes per platform is
only where the request goes, with which headers, and where the PNG is in the
answer. That is the target, serialized beside the protocol.

The generator runs on the loadgen VM. Its event ledger is fetched whatever k6
does, including when it fails: a cell's requests are its evidence, and a
generator that crashed halfway still sent the first half.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from sonata_tasks.k6 import k6_argv
from sonata_tasks.k6_models import K6Config

from nanolab.comparison.experiment_01 import EVENTS, SUMMARY
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
    protocol_values = {
        "phases": protocol.phases(),
        "httpTimeoutSeconds": protocol.http_timeout_seconds,
        "gracefulStopSeconds": protocol.graceful_stop_seconds,
        "preAllocatedVUs": protocol.pre_allocated_vus,
        "maxVUs": protocol.max_vus,
    }
    target_values = {
        "url": target.url,
        "headers": dict(target.headers),
        "extractor": target.extractor,
        "field": target.field,
        "pngSha256": protocol.png_sha256,
        "size": json.loads(payload)["size"],
    }
    return {
        "E02_PAYLOAD": body.decode("utf-8"),
        "E02_PROTOCOL": json.dumps(
            protocol_values, sort_keys=True, separators=(",", ":")
        ),
        "E02_TARGET": json.dumps(target_values, sort_keys=True, separators=(",", ":")),
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
