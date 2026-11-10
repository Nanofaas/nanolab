"""A failed or unverified final collection must retain the source VM."""

import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nanolab.tasks.resource_tasks import resource_of
from nanolab.workbench.components import Deployed
from nanolab.workbench.inventory import Instance
from nanolab.workbench.playbook import WorkbenchHost


@pytest.mark.parametrize(
    "fault", [None, "checksum", "quiescence", "missing", "export", "stale"]
)
def test_final_collection_requires_verified_quiesced_archive(
    tmp_path, monkeypatch, fault
):
    from nanolab.workbench import evidence

    node = SimpleNamespace(
        config=SimpleNamespace(id="edge"),
        request=SimpleNamespace(name="owned-vm"),
        recovery=[],
        vm=SimpleNamespace(revive=lambda raw: raw),
    )
    value = Deployed("edge", "collector-edge", "log-collector", None, deployed_at=1)
    resource = resource_of("collector", lambda _: value, lambda *_: None)
    source = Deployed(
        "edge", "node-exporter-edge", "node-exporter", None, deployed_at=0.5
    )
    since = resource_of("first source", lambda _: source, lambda *_: None)
    instance = Instance(
        name="collector-edge",
        component="log-collector",
        config={"storage_dir": "/buffer"},
    )
    directory = tmp_path / "workbench/edge/collector-edge"
    modes = []
    if fault == "stale":
        directory.mkdir(parents=True)
        (directory / "storage.tar.gz").write_bytes(b"previous attempt")
        (directory / "collection.json").write_text(
            json.dumps(
                {
                    "mode": "final",
                    "quiesced": True,
                    "archiveSha256": hashlib.sha256(b"previous attempt").hexdigest(),
                }
            )
        )

    def collect(host, instance, deployed, **kwargs):
        mode = instance.config["collect_mode"]
        modes.append(mode)

        def run(inputs):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "metrics.txt").write_text(
                "fluentbit_input_records_total 0\n"
                "fluentbit_output_proc_records_total 0\n"
                "fluentbit_output_dropped_records_total 0\n"
            )
            if mode == "final":
                if fault in {"missing", "stale"}:
                    raise RuntimeError("collection transport failed")
                archive = directory / "storage.tar.gz"
                archive.write_bytes(b"recoverable buffers")
                (directory / "container-evidence.json").write_text(
                    json.dumps({"logsExitCode": 1, "statsExitCode": 0})
                )
                (directory / "collection.json").write_text(
                    json.dumps(
                        {
                            "mode": "final",
                            "quiesced": fault != "quiescence",
                            "metricsAvailable": False,
                            "containerEvidenceError": "diagnostic timeout",
                            "archiveSha256": "bad"
                            if fault in {"checksum", "export"}
                            else hashlib.sha256(archive.read_bytes()).hexdigest(),
                        }
                    )
                )
                raise RuntimeError("metrics unavailable")  # Raw recovery still ran.

        return SimpleNamespace(run=run)

    monkeypatch.setattr(evidence, "collect_deployed", collect)
    monkeypatch.setattr(evidence.time, "sleep", lambda _: None)

    def export(**kwargs):
        if fault is None:
            assert kwargs["session"].started_ns == 500_000_000
            assert any("diagnostic timeout" in issue for issue in kwargs["issues"])
            assert any("logsExitCode=1" in issue for issue in kwargs["issues"])
        if fault == "export":
            raise RuntimeError("export failed too")

    monkeypatch.setattr(evidence, "export_collector", export)
    task = evidence.CollectorEvidence(
        WorkbenchHost("edge", cast(Any, node)),
        instance,
        resource,
        since=since,
        run_dir=tmp_path,
        vars_dir=tmp_path / "vars",
        storage_dir="/buffer",
        logs=cast(Any, SimpleNamespace()),
        settings=None,
        budget=lambda: {},
    )
    inputs = cast(
        Any,
        SimpleNamespace(
            upstream=lambda: SimpleNamespace(
                of=lambda r: source if r is since else value
            )
        ),
    )
    if fault:
        with pytest.raises(RuntimeError, match="state archive") as caught:
            task.run(inputs)
        assert node.recovery
        assert (tmp_path / "logging/edge/recovery.json").exists()
        if fault == "export":
            assert any("export failed too" in note for note in caught.value.__notes__)
    else:
        task.run(inputs)
        assert not node.recovery
        assert (
            tmp_path / "logging/edge/collector-state.tar.gz"
        ).read_bytes() == b"recoverable buffers"
    assert modes == ["metrics", "final"]
