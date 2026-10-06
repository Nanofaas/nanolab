from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

from tests.tasks.recipes.test_validation import distribution


@pytest.mark.parametrize("failure", [None, "version", "image", "exit", "create"])
def test_watchdog_probe_verifies_artifact_and_cleans_only_owned_container(
    tmp_path: Path, failure: str | None
) -> None:
    from nanolab.tasks.recipes.validation import recipe_watchdog_resource

    original = distribution(tmp_path)
    service = replace(
        original.components[1],
        kind="service",
        name="watchdog",
        sdk="dockerfile",
        mode="container",
        image=replace(
            original.components[1].image,
            reference="registry/published-watchdog:run-1",
            id="sha256:watchdog",
        ),
    )
    value = replace(original, components=(*original.components, service))
    selected = Resource(
        title="published recipe",
        acquire=lambda _inputs: value,
        release=lambda _inputs, _value: None,
    )
    inputs = TaskInputs._for_resources({selected: value}, {selected})
    source = tmp_path / "source"
    manifest = source / "runtimes/watchdog/Cargo.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[package]\nversion = "0.1.0"\n')
    commands: list[CommandTaskSpec] = []
    owned_id = "a" * 64

    class Executor:
        def binding_key(self, role: str) -> str:
            return role

        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            commands.append(task)
            operation = task.argv[1]
            stdout = {
                "create": owned_id,
                "start": "incorrect"
                if failure == "version"
                else "nanofaas-watchdog 0.1.0\n",
                "inspect": json.dumps(
                    {
                        "Image": "sha256:wrong"
                        if failure == "image"
                        else "sha256:watchdog",
                        "State": {"ExitCode": 7 if failure == "exit" else 0},
                    }
                ),
                "rm": owned_id,
            }[operation]
            failed = failure == "create" and operation == "create"
            return TaskResult(
                task_id="",
                status="failed" if failed else "passed",
                return_code=1 if failed else 0,
                stdout=stdout,
                stderr="container name occupied" if failed else "",
            )

    probe = recipe_watchdog_resource(
        distribution=selected,
        executor=Executor(),
        source=source,
        run_dir=tmp_path,
        container="existing-name",
    )
    if failure:
        with pytest.raises(RuntimeError, match=r"watchdog|occupied|create"):
            probe.acquire(inputs)
    else:
        result = probe.acquire(inputs)
        probe.release(inputs, result)
    create = commands[0]
    assert create.argv[-2:] == ("registry/published-watchdog:run-1", "--version")
    removals = [task.argv for task in commands if task.argv[1] == "rm"]
    assert removals == (
        [] if failure == "create" else [("docker", "rm", "-f", owned_id)]
    )
    if failure != "create":
        evidence = json.loads((tmp_path / "image-service-watchdog.json").read_text())
        assert evidence["container"] == owned_id
        assert evidence["reference"] == "registry/published-watchdog:run-1"
        assert evidence["expectedVersion"] == "nanofaas-watchdog 0.1.0"
