"""Exercise the image task through Sonata's real resource-input contract."""

from types import SimpleNamespace

import pytest
from sonata_engine import Resource, Workflow

from nanolab.tasks.soak.builds import BuildImagesTask


def test_build_resolves_declared_snapshot_before_materialization(tmp_path, monkeypatch):
    snapshot_value = SimpleNamespace(fingerprint="snapshot-fingerprint")
    snapshot = Resource(
        title="Acquire source snapshot",
        acquire=lambda _inputs: snapshot_value,
        release=lambda _inputs, _value: None,
    )
    received = []

    def materialize(value, destination):
        received.append(value)
        raise RuntimeError("materialization sentinel")

    monkeypatch.setattr("nanolab.tasks.soak.builds.materialize_snapshot", materialize)
    recipe = SimpleNamespace(role="control-plane", mode="build", bake={})
    task = BuildImagesTask(
        (recipe,),  # pyright: ignore[reportArgumentType]
        snapshot=snapshot,  # pyright: ignore[reportArgumentType]
        executor=None,  # pyright: ignore[reportArgumentType]
        output_dir=tmp_path / "builds",
        collect=lambda *_args: None,  # pyright: ignore[reportArgumentType]
        artifact_limit_bytes=1024 * 1024,
    )
    workflow = Workflow(workflow_id="build-resource-contract")
    workflow.add(task, requires=(snapshot,))
    with pytest.raises(RuntimeError, match="materialization sentinel"):
        workflow.run()
    assert received == [snapshot_value]
