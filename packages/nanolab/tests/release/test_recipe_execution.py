"""Archive changes, incomplete transfers and stale tags must stop assembly evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.images.plan import ImageArchitecture, build_image_plan
from nanolab.release import recipe_execution as execution
from nanolab.release.resources import release_recipe_inputs_resource

from .test_recipe import _groups, _report

ROOT = "/home/azureuser/nanofaas-release/v9.9.9"


class LocalProvider:
    """Run transport commands on real files; substitute only the VM root."""

    def __init__(self, root: Path, architecture: ImageArchitecture = "amd64"):
        self.root = root
        self.architecture = architecture
        self.host = "x86_64" if architecture == "amd64" else "aarch64"
        self.host_os = "Linux"
        self.daemon = "linux|" + architecture
        self.groups = _groups(architecture=architecture)
        self.truncate = False
        self.fail_group = ""
        self.fail_cleanup = False
        self.image_override = ""
        self.replace_earlier_tag = False
        self.replace_after_default = False
        self.commands = []

    def path(self, value: str) -> str:
        return value.replace(ROOT, str(self.root))

    def exec_argv(self, request, argv, *, env=None, remote_dir=None, dry_run=False):
        self.commands.append(argv)
        if self.fail_cleanup and argv[:2] == ("rm", "-rf"):
            return SimpleNamespace(return_code=1, stdout="", stderr="cleanup failed")
        if argv[:3] == ("docker", "image", "inspect"):
            refs = argv[4:]
            ids = {
                r["image"]["reference"]: r["image"]["id"]
                for g in self.groups
                for r in _report(g)["components"]
                if r["image"] is not None
            }
            return SimpleNamespace(
                return_code=0,
                stderr="",
                stdout="\n".join(
                    self.image_override
                    or (
                        f"linux|{self.architecture}|sha256:" + "f" * 64
                        if self.replace_earlier_tag and ref.endswith("-jvm")
                        else ids[ref]
                        if argv[3] == "--format={{.Id}}"
                        else f"linux|{self.architecture}|{ids[ref]}"
                    )
                    for ref in refs
                ),
            )
        if argv[0] in {"uname", "docker"}:
            return SimpleNamespace(
                return_code=0,
                stderr="",
                stdout=(
                    self.host_os + "\n"
                    if argv == ("uname", "-s")
                    else self.host + "\n"
                    if argv[0] == "uname"
                    else self.daemon + "\n"
                    if argv[:2] == ("docker", "info")
                    else "Driver: docker-container\nBuildKit version: v0.20\n"
                    if argv[:3] == ("docker", "buildx", "inspect")
                    else "version 1\n"
                ),
            )
        result = subprocess.run(
            tuple(self.path(a) for a in argv),
            cwd=self.path(remote_dir) if remote_dir else None,
            env=os.environ | dict(env or {}),
            text=True,
            capture_output=True,
            check=False,
        )
        return SimpleNamespace(
            return_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    def transfer_to(self, request, *, source, destination):
        shutil.copyfile(source, self.path(destination))
        return SimpleNamespace(return_code=0)

    def transfer_from(self, request, *, source, destination):
        data = Path(self.path(source)).read_bytes()
        Path(destination).write_bytes(data[:-1] if self.truncate else data)
        return SimpleNamespace(return_code=0)


class RecipeExecutor:
    def __init__(self, provider):
        self.provider = provider
        self.commands = []

    def run(self, task, *, dry_run=False):
        self.commands.append(task)
        # Simulate the external Gradle producer, preserving independent transport/files.
        group = next(g for g in self.provider.groups if g.name in task.argv[-1])
        output = (
            self.provider.root
            / "recipe-output"
            / self.provider.architecture
            / group.flavor
        )
        assert not (output / "distribution.json").exists()
        output.mkdir(parents=True, exist_ok=True)
        (
            self.provider.root
            / "recipe-output"
            / self.provider.architecture
            / "logs"
            / f"{group.flavor}.log"
        ).write_text("complete Gradle log\n")
        if group.flavor == self.provider.fail_group:
            raise RuntimeError("assembly failed")
        (output / "distribution.json").write_text(json.dumps(_report(group)))
        if (
            getattr(self.provider, "replace_after_default", False)
            and group.flavor == "default"
        ):
            self.provider.replace_earlier_tag = True
        return SimpleNamespace(return_code=0, stdout="", stderr="")


@pytest.fixture
def staged(tmp_path):
    source = tmp_path / "vm" / "source"
    source.mkdir(parents=True)
    (source / "build.gradle").write_text("plugins {}\n")
    (source / "settings.gradle").write_text(
        'rootProject.name="probe"\ninclude("project")\n'
    )
    (source / "project").mkdir()
    (source / "project" / "build.gradle").write_text("plugins {}\n")
    (source / "project" / "build").mkdir()
    (source / "project" / "build" / "committed").write_text("guarded\n")
    (source / "link").symlink_to("project/build.gradle")
    inventory = execution.capture_release_inventory(source, tmp_path / "inventory.json")
    return source, inventory


@pytest.mark.nanofaas
def test_release_recipe_commands_use_owned_builder_and_paths():
    groups = _groups()
    commands = execution.release_recipe_commands(
        groups, source_dir=ROOT + "/source", remote_root=ROOT, builder_name="owned"
    )
    assert len(commands) == 3
    for command, group in zip(commands, groups, strict=True):
        assert command.argv[:2] == ("./gradlew", "assembleRecipe")
        assert f"-Precipe={ROOT}/recipe-inputs/amd64/{group.name}.yaml" in command.argv
        assert (
            f"-PrecipeOutput={ROOT}/recipe-output/amd64/{group.flavor}" in command.argv
        )
        assert f"-PrecipeTag={group.tag}" in command.argv
        assert command.role == "stack"
        assert command.options.remote_dir == ROOT + "/source"
        assert command.options.env == {
            "DOCKER_BUILDKIT": "1",
            "BUILDX_BUILDER": "owned",
        }
        assert not any(
            "bake" in a or "bootJar" in a or "publish" in a or "recipeBuilder" in a
            for a in command.argv
        )


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "mode",
        "delete",
        "link",
        "escape",
        "ignored",
        "tracked-output",
        "non-project-build",
        "output-link",
    ],
)
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_archive_inventory_rejects_source_mutation(staged, mutation, architecture):
    source, inventory = staged
    if mutation == "bytes":
        (source / "build.gradle").write_text("changed")
    elif mutation == "mode":
        (source / "build.gradle").chmod(0o755)
    elif mutation == "delete":
        (source / "build.gradle").unlink()
    elif mutation in {"link", "escape"}:
        (source / "link").unlink()
        (source / "link").symlink_to(
            "../escape" if mutation == "escape" else "settings.gradle"
        )
    elif mutation == "ignored":
        (source / "ignored-source.java").write_text("added")
    elif mutation == "tracked-output":
        (source / "project/build/committed").write_text("changed")
    elif mutation == "non-project-build":
        (source / "unknown/build").mkdir(parents=True)
        (source / "unknown/build/new").write_text("added")
    else:
        (source / ".gradle").mkdir()
        (source / ".gradle/escape").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match=r"source|inventory|symlink"):
        execution.verify_release_source(
            inventory_file=inventory,
            provider=LocalProvider(source.parent, architecture),
            request=object(),
            source_dir=ROOT + "/source",
        )


@pytest.mark.nanofaas
def test_inventory_allows_only_source_derived_gradle_outputs(staged):
    source, inventory = staged
    (source / ".gradle/cache").mkdir(parents=True)
    (source / ".gradle/cache/new").write_text("cache")
    (source / "project/build/classes").mkdir()
    (source / "project/build/classes/new").write_text("class")
    execution.verify_release_source(
        inventory_file=inventory,
        provider=LocalProvider(source.parent),
        request=object(),
        source_dir=ROOT + "/source",
    )
    second = execution.capture_release_inventory(
        source, inventory.parent / "second.json"
    )
    assert json.loads(second.read_text())["entries"]["project/build/committed"][
        "sha256"
    ]


def _run(
    staged,
    tmp_path,
    *,
    image_override="",
    fail_group="",
    truncate=False,
    corrupt_archive=False,
    corrupt_config=False,
    replace_after_default=False,
    architecture: ImageArchitecture = "amd64",
    host=None,
    host_os=None,
    daemon=None,
):
    source, inventory = staged
    archive_bytes = b"verified archive"
    (source.parent / "source.tar").write_bytes(
        b"changed" if corrupt_archive else archive_bytes
    )
    provider = LocalProvider(source.parent, architecture)
    provider.image_override, provider.fail_group, provider.truncate = (
        image_override,
        fail_group,
        truncate,
    )
    provider.replace_after_default = replace_after_default
    if host is not None:
        provider.host = host
    if host_os is not None:
        provider.host_os = host_os
    if daemon is not None:
        provider.daemon = daemon
    executor = RecipeExecutor(provider)
    local = tmp_path / "evidence"
    local.mkdir(exist_ok=True)
    config = local / f"buildkitd-{architecture}.toml"
    config.write_text("[worker.oci]\n  max-parallelism = 2\n")
    remote = source.parent / f"recipe-inputs/{architecture}"
    remote.mkdir(parents=True, exist_ok=True)
    for group in provider.groups:
        (remote / f"{group.name}.yaml").write_bytes(group.profile_bytes)
        (local / f"{group.name}.yaml").write_bytes(group.profile_bytes)
    (remote / config.name).write_bytes(
        b"changed" if corrupt_config else config.read_bytes()
    )
    result = execution.run_release_recipe_steps(
        cast(TaskInputs, object()),
        groups=provider.groups,
        executor=cast(RoleBoundCommandTaskExecutor, executor),
        provider=provider,
        request=object(),
        source_dir=ROOT + "/source",
        remote_root=ROOT,
        evidence_dir=local,
        inventory_file=inventory,
        source_commit="a" * 40,
        archive_digest="sha256:" + hashlib.sha256(archive_bytes).hexdigest(),
        builder_name="owned-builder",
        architecture=architecture,
        role="arm-builder" if architecture == "arm64" else "stack",
    )
    return result, provider, executor


@pytest.mark.nanofaas
def test_complete_reports_match_independent_daemon_inspection(staged, tmp_path):
    evidence, provider, executor = _run(staged, tmp_path)
    images = [e for e in evidence if e.kind == "local-image-digest"]
    assert len(images) == 48
    assert all(
        e.reference.startswith("docker-daemon:127.0.0.1:5000/nanofaas/") for e in images
    )
    files = [e for e in evidence if e.kind == "file-digest"]
    assert len([e for e in files if e.reference.endswith("distribution.json")]) == 3
    assert len([e for e in files if e.reference.endswith("gradle.log")]) == 3
    assert any(e.reference == str(staged[1]) for e in files)
    assert any(e.reference.endswith("build-facts.json") for e in files)
    assert len(executor.commands) == 3
    assert all(
        c.options.env["BUILDX_BUILDER"] == "owned-builder" for c in executor.commands
    )
    assert (
        len([a for a in provider.commands if a[:3] == ("docker", "image", "inspect")])
        == 4
    )


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    "value",
    [
        "windows|amd64|sha256:" + "1" * 64,
        "linux|arm64|sha256:" + "1" * 64,
        "linux|amd64|sha256:" + "f" * 64,
    ],
)
def test_daemon_disagreement_prevents_receipt(staged, tmp_path, value):
    with pytest.raises(ValueError, match=r"image|daemon|platform"):
        _run(staged, tmp_path, image_override=value)


@pytest.mark.nanofaas
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_failed_group_cannot_reuse_stale_output(staged, tmp_path, architecture):
    stale = staged[0].parent / f"recipe-output/{architecture}/native"
    stale.mkdir(parents=True)
    (stale / "distribution.json").write_text("old successful report")
    with pytest.raises(RuntimeError, match="assembly failed"):
        _run(staged, tmp_path, architecture=architecture, fail_group="native")
    assert not (stale / "distribution.json").exists()
    assert (tmp_path / "evidence/jvm/distribution.json").exists()
    assert (
        tmp_path / "evidence/native/gradle.log"
    ).read_text() == "complete Gradle log\n"
    assert not (tmp_path / "evidence/native/distribution.json").exists()


@pytest.mark.nanofaas
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_transfer_truncation_prevents_receipt(staged, tmp_path, architecture):
    with pytest.raises(ValueError, match=r"transfer|digest|size"):
        _run(staged, tmp_path, architecture=architecture, truncate=True)


@pytest.mark.nanofaas
@pytest.mark.parametrize("failure", ["transfer", "cancel", "cleanup"])
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_partial_recipe_failure_compensates_owned_inputs(
    tmp_path, failure, architecture
):
    from dataclasses import dataclass

    from sonata_engine import Task, TaskOutcome, Workflow

    class FailingProvider(LocalProvider):
        def transfer_to(self, request, *, source, destination):
            if source.name.endswith("native.yaml"):
                if failure == "cancel":
                    raise KeyboardInterrupt("cancelled")
                return SimpleNamespace(return_code=1, stderr="transfer failed")
            return super().transfer_to(request, source=source, destination=destination)

    root = tmp_path / "vm"
    root.mkdir()
    unrelated = root / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("keep")
    provider = FailingProvider(root, architecture)
    provider.fail_cleanup = failure == "cleanup"
    resource = release_recipe_inputs_resource(
        groups=provider.groups,
        architecture=architecture,
        max_parallelism=2,
        run_dir=tmp_path / "evidence",
        remote_root=ROOT,
        provider=provider,
        request=object(),
    )

    @dataclass
    class Consume(Task[None]):
        title: str = "Consume recipe inputs"

        def run(self, inputs):
            inputs.resource(resource)
            return TaskOutcome()

    workflow = Workflow("recipe-input-cleanup")
    workflow.add(Consume(), requires=(resource,))
    # Resource acquisition compensates even before the engine can own its result.
    expected = KeyboardInterrupt if failure == "cancel" else RuntimeError
    with pytest.raises(expected) as caught:
        workflow.run()
    if failure == "cleanup":
        assert any("cleanup failed" in note for note in caught.value.__notes__)
    else:
        assert not (root / f"recipe-inputs/{architecture}").exists()
    assert (unrelated / "keep").read_text() == "keep"
    assert (tmp_path / f"evidence/release-{architecture}-jvm.yaml").exists()
    assert (tmp_path / f"evidence/buildkitd-{architecture}.toml").exists()


@pytest.mark.nanofaas
def test_recipe_input_release_preserves_local_evidence(tmp_path):
    from dataclasses import dataclass

    from sonata_engine import Task, TaskOutcome, Workflow

    root = tmp_path / "vm"
    root.mkdir()
    provider = LocalProvider(root)
    resource = release_recipe_inputs_resource(
        groups=provider.groups,
        max_parallelism=2,
        run_dir=tmp_path / "evidence",
        remote_root=ROOT,
        provider=provider,
        request=object(),
    )

    @dataclass
    class Consume(Task[None]):
        title: str = "Consume recipe inputs"

        def run(self, inputs):
            assert (
                inputs.resource(resource).read_text()
                == "[worker.oci]\n  max-parallelism = 2\n"
            )
            return TaskOutcome()

    workflow = Workflow("recipe-input-release")
    workflow.add(Consume(), requires=(resource,))
    workflow.run()
    assert not (root / "recipe-inputs/amd64").exists()
    assert (tmp_path / "evidence/buildkitd-amd64.toml").exists()
    assert len(list((tmp_path / "evidence").glob("*.yaml"))) == 3


@pytest.mark.nanofaas
@pytest.mark.parametrize("which", ["archive", "config"])
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_acquired_input_digest_mismatch_prevents_assembly(
    staged, tmp_path, which, architecture
):
    with pytest.raises(ValueError, match=r"archive|config"):
        _run(
            staged,
            tmp_path,
            architecture=architecture,
            corrupt_archive=which == "archive",
            corrupt_config=which == "config",
        )


@pytest.mark.nanofaas
def test_inventory_allows_guarded_included_build_cache(staged):
    source, inventory = staged
    (source / "settings.gradle").write_text("includeBuild('plugin')\n")
    (source / "plugin").mkdir()
    (source / "plugin/settings.gradle").write_text('rootProject.name="plugin"\n')
    (source / "plugin/build.gradle").write_text("plugins {}\n")
    execution.capture_release_inventory(source, inventory)
    (source / "plugin/.gradle").mkdir()
    (source / "plugin/.gradle/cache").write_text("output")
    execution.verify_release_source(
        inventory_file=inventory,
        provider=LocalProvider(source.parent),
        request=object(),
        source_dir=ROOT + "/source",
    )


@pytest.mark.nanofaas
def test_final_inspection_rejects_earlier_tag_replaced_by_later_group(staged, tmp_path):
    with pytest.raises(ValueError, match="image"):
        _run(staged, tmp_path, replace_after_default=True)


@pytest.mark.nanofaas
def test_archive_staging_matches_planning_permissions_despite_vm_umask(tmp_path):
    import io
    import tarfile

    from nanolab.release.build import stage_source_archive
    from nanolab.release.model import digest_path

    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as bundle:
        directory = tarfile.TarInfo("project")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o775
        bundle.addfile(directory)
        file = tarfile.TarInfo("project/build.gradle")
        file.size = 3
        file.mode = 0o664
        bundle.addfile(file, io.BytesIO(b"abc"))
    expected = tmp_path / "expected"
    expected.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(expected, filter="data")
    (expected / "project").chmod(0o755)
    inventory = execution.capture_release_inventory(
        expected, tmp_path / "inventory.json"
    )
    remote = tmp_path / "vm"
    remote.mkdir()
    provider = LocalProvider(remote)
    stage_source_archive(
        provider,
        object(),
        archive=archive,
        remote_archive=ROOT + "/source.tar",
        remote_source_dir=ROOT + "/source",
        expected_digest=digest_path(archive),
    )
    execution.verify_release_source(
        inventory_file=inventory,
        provider=provider,
        request=object(),
        source_dir=ROOT + "/source",
    )


def _recipe_phase(
    staged, tmp_path, *, architecture: ImageArchitecture = "amd64", **overrides
):
    from nanolab.plans.release_phases import build_recipe_assembly_phase
    from nanolab.release.model import ReleaseIdentity, digest_path
    from nanolab.release.tasks import source_test_task

    source, inventory = staged
    provider = LocalProvider(source.parent, architecture)
    archive = source.parent / "source.tar"
    archive.write_bytes(b"guarded source")
    run_dir = tmp_path / "release-run"
    release_dir = run_dir / "releases/9.9.9"
    inputs = release_dir / f"recipe-inputs/{architecture}"
    inputs.mkdir(parents=True)
    remote = source.parent / f"recipe-inputs/{architecture}"
    remote.mkdir(parents=True)
    for g in provider.groups:
        for directory in (inputs, remote):
            (directory / f"{g.name}.yaml").write_bytes(g.profile_bytes)
    for directory in (inputs, remote):
        (directory / f"buildkitd-{architecture}.toml").write_text(
            "[worker.oci]\n  max-parallelism = 2\n"
        )
    identity = ReleaseIdentity(
        "a" * 40, "9.9.9", "sha256:" + "c" * 64, "sha256:" + "d" * 64
    )
    source_tests = source_test_task(
        identity=identity, run_dir=run_dir, phase_inputs={}, work=lambda _: ()
    )
    source_tests.receipt.parent.mkdir(parents=True, exist_ok=True)
    source_tests.receipt.write_text("{}")
    executor = RecipeExecutor(provider)
    arguments: dict[str, Any] = {
        "architecture": architecture,
        "role": "stack" if architecture == "amd64" else "arm-builder",
        "identity": identity,
        "run_dir": run_dir,
        "image_plan": build_image_plan(
            Path(os.environ["NANOFAAS_ROOT"]), "v9.9.9", architectures=(architecture,)
        ),
        "max_parallelism": 2,
        "builder_name": "owned-builder",
        "remote_root": ROOT,
        "source_dir": ROOT + "/source",
        "executor": cast(RoleBoundCommandTaskExecutor, executor),
        "prerequisite_phases": (source_tests,),
        "recipe_groups": provider.groups,
        "provider": provider,
        "request": object(),
        "inventory_file": inventory,
        "archive_digest": digest_path(archive),
    }
    arguments.update(overrides)
    _, phase = build_recipe_assembly_phase(**arguments)
    return phase, provider, executor, source_tests, arguments


@pytest.mark.nanofaas
def test_recipe_execution_retains_input_copies_only_when_build_runs(staged, tmp_path):
    phase, provider, _executor, _source_tests, arguments = _recipe_phase(
        staged, tmp_path
    )
    release_dir = arguments["run_dir"] / "releases/9.9.9"
    inputs = release_dir / "recipe-inputs/amd64"
    outcome = phase.run(cast(TaskInputs, object()))
    files = [Path(e.reference) for e in outcome.evidence if e.kind == "file-digest"]
    assert any(
        p.name == "release-amd64-jvm.yaml" and "recipe-evidence" in str(p)
        for p in files
    )
    retained = release_dir / "recipe-evidence/amd64/release-amd64-jvm.yaml"
    retained.write_text("tampered")
    # Acquiring transient inputs must not rewrite retained receipt files.
    assert (inputs / "release-amd64-jvm.yaml").read_bytes() == provider.groups[
        0
    ].profile_bytes
    assert retained.read_text() == "tampered"
    assert not any(str(inputs) in e.reference for e in outcome.evidence)


@pytest.mark.parametrize("fail_create", [False, True])
@pytest.mark.parametrize("role", ["stack", "arm-builder"])
def test_owned_builder_preserves_previous_selection(fail_create, role):
    from dataclasses import dataclass

    from sonata_engine import Task, TaskOutcome, Workflow
    from sonata_tasks.buildx import buildx_builder_resource
    from sonata_tasks.execution.models import TaskResult

    from nanolab.release import resources

    class SelectedExecutor:
        selected = "unrelated-builder"

        def binding_key(self, role):
            return "selected:" + role

        def run(self, task, *, dry_run=False):
            assert task.role == role
            args = task.argv[2:]
            if args == ("inspect",):
                return TaskResult("", "passed", 0, stdout="Name: unrelated-builder\n")
            if args == ("inspect", "owned"):
                return TaskResult("", "passed", 1)
            if args[0] == "create":
                self.selected = "owned"
                if fail_create:
                    return TaskResult("", "failed", 1, stderr="create failed")
            if args[0] == "use":
                self.selected = args[-1]
            if args[0] == "rm":
                assert args[-1] == "owned"
            return TaskResult("", "passed", 0)

    executor = SelectedExecutor()
    original = buildx_builder_resource(
        name="owned",
        executor=executor,
        role=role,
        driver_options=("default-load=true",),
    )
    owned = resources.preserve_release_builder_selection(
        original, executor=executor, name="owned", role=role
    )

    @dataclass
    class Consume(Task[None]):
        title: str = "Consume owned builder"

        def run(self, inputs):
            inputs.resource(owned)
            assert executor.selected == "unrelated-builder"
            return TaskOutcome()

    workflow = Workflow("owned-builder-selection")
    workflow.add(Consume(), requires=(owned,))
    if fail_create:
        with pytest.raises(RuntimeError, match="create failed"):
            workflow.run()
    else:
        workflow.run()
    assert executor.selected == "unrelated-builder"


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    "changed",
    [
        "profile",
        "tag",
        "archive",
        "inventory",
        "cell-options",
        "env",
        "cwd",
        "builder",
        "parallelism",
        "driver",
        "configuration",
    ],
)
def test_recipe_phase_fingerprint_binds_all_inputs(
    staged, tmp_path, monkeypatch, changed
):
    from dataclasses import replace

    from nanolab.plans import release_phases

    phase, provider, _executor, _source, arguments = _recipe_phase(staged, tmp_path)
    original_key = phase.reuse_key
    if changed in {"driver", "configuration"}:
        key = "driverOptions" if changed == "driver" else "buildkitConfigDigest"
        other = replace(phase, phase_inputs={**phase.phase_inputs, key: "changed"})
    else:
        args = dict(arguments)
        if changed == "archive":
            args["archive_digest"] = "sha256:" + "f" * 64
        elif changed == "inventory":
            with args["inventory_file"].open("a") as f:
                f.write("\n")
        elif changed == "builder":
            args["builder_name"] = "another-owned"
        elif changed == "parallelism":
            args["max_parallelism"] = 3
        elif changed in {"profile", "tag", "cell-options"}:
            group = provider.groups[0]
            if changed == "profile":
                data = group.profile_bytes + b"# harmless recipe comment\n"
                group = replace(
                    group,
                    profile_bytes=data,
                    profile_digest="sha256:" + hashlib.sha256(data).hexdigest(),
                )
            elif changed == "tag":
                group = replace(group, tag=group.tag + "-changed")
            else:
                cell = group.cells[0]
                target = replace(
                    cell.target,
                    jvm_prerequisite_arguments=(
                        ":control-plane:bootJar",
                        "-Pchanged=true",
                    ),
                )
                group = replace(
                    group, cells=(replace(cell, target=target), *group.cells[1:])
                )
            args["recipe_groups"] = (group, *provider.groups[1:])
        else:
            original = release_phases.release_recipe_commands

            def commands(*a, **kw):
                result = list(original(*a, **kw))
                command = result[0]
                options = replace(
                    command.options,
                    **(
                        {"env": dict(command.options.env) | {"DOCKER_BUILDKIT": "0"}}
                        if changed == "env"
                        else {"remote_dir": ROOT + "/other-source"}
                    ),
                )
                result[0] = replace(command, options=options)
                return tuple(result)

            monkeypatch.setattr(release_phases, "release_recipe_commands", commands)
        _, other = release_phases.build_recipe_assembly_phase(**args)
    assert other.reuse_key != original_key


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "report-tamper",
        "report-delete",
        "profile",
        "inventory",
        "image-id",
        "receipt",
    ],
)
def test_recipe_resume_verifies_files_and_images_without_build(
    staged, tmp_path, mutation
):
    from sonata_engine import Evidence, JournalConfig, Workflow

    from nanolab.release.evidence import file_digest_verifier, image_digest_verifier
    from nanolab.release.model import digest_path
    from nanolab.release.tasks import benchmark_task, registry_push_task

    phase, provider, executor, source, arguments = _recipe_phase(staged, tmp_path)
    counts = {"push": 0, "benchmark": 0}
    registry_digest = "sha256:" + "e" * 64

    def push_work(_inputs):
        counts["push"] += 1
        return tuple(
            Evidence("local-registry-digest", "docker://" + c.image, registry_digest)
            for g in provider.groups
            for c in g.cells
        )

    push = registry_push_task(
        identity=phase.identity,
        run_dir=arguments["run_dir"],
        phase_inputs={},
        prerequisites=(source.receipt, phase.receipt),
        expected_images=phase.expected_images,
        work=push_work,
    )
    benchmark_file = tmp_path / "benchmark.json"
    benchmark_file.write_text("{}")

    def benchmark_work(_inputs):
        counts["benchmark"] += 1
        return (
            Evidence("file-digest", str(benchmark_file), digest_path(benchmark_file)),
        )

    benchmarks = [
        benchmark_task(
            i,
            identity=phase.identity,
            run_dir=arguments["run_dir"],
            phase_inputs={},
            prerequisites=(phase.receipt, push.receipt),
            work=benchmark_work,
        )
        for i in range(1, 4)
    ]
    workflow = Workflow("recipe-resume")
    workflow.add(phase)
    workflow.add(push)
    for benchmark in benchmarks:
        workflow.add(benchmark)
    ids = {
        r["image"]["reference"]: r["image"]["id"]
        for g in provider.groups
        for r in _report(g)["components"]
        if r["image"]
    }

    def image_id(reference):
        image = reference.removeprefix("docker-daemon:")
        return (
            "sha256:" + "f" * 64
            if provider.replace_earlier_tag and image.endswith("-jvm")
            else ids[image]
        )

    verifiers = {
        "file-digest": file_digest_verifier,
        "local-image-digest": image_digest_verifier(image_id),
        "local-registry-digest": lambda e: e.digest == registry_digest,
    }
    journal = JournalConfig(tmp_path / "journal.jsonl")
    workflow.run(journal=journal, verifiers=verifiers)
    assert len(executor.commands) == 3 and counts == {"push": 1, "benchmark": 3}
    evidence = arguments["run_dir"] / "releases/9.9.9/recipe-evidence/amd64"
    if mutation == "report-tamper":
        (evidence / "jvm/distribution.json").write_text("{}")
    elif mutation == "report-delete":
        (evidence / "jvm/distribution.json").unlink()
    elif mutation == "profile":
        (evidence / "release-amd64-jvm.yaml").write_text("changed")
    elif mutation == "inventory":
        (evidence / "source-inventory.json").write_text("{}")
    elif mutation == "image-id":
        provider.replace_earlier_tag = True
    elif mutation == "receipt":
        phase.receipt.write_text("{}")
    # The fake external producer restores the tag, as a real assembly would.
    original = executor.run

    def build(task, **kw):
        provider.replace_earlier_tag = False
        return original(task, **kw)

    executor.run = build
    workflow.run(journal=journal, verifiers=verifiers, resume=True)
    expected = 3 if mutation == "none" else 6
    assert len(executor.commands) == expected
    assert counts == (
        {"push": 1, "benchmark": 3}
        if mutation == "none"
        else {"push": 2, "benchmark": 6}
    )


@pytest.mark.nanofaas
def test_partial_recipe_outcome_removes_prior_complete_receipt(staged, tmp_path):
    phase, provider, _executor, _source, args = _recipe_phase(staged, tmp_path)
    phase.run(cast(TaskInputs, object()))
    assert phase.receipt.is_file()
    provider.fail_group = "native"
    with pytest.raises(RuntimeError, match="assembly failed"):
        phase.run(cast(TaskInputs, object()))
    assert not phase.receipt.exists()
    assert (
        args["run_dir"] / "releases/9.9.9/recipe-evidence/amd64/native/gradle.log"
    ).exists()


@pytest.mark.nanofaas
def test_inventory_rejects_build_outputs_from_undeclared_gradle_project(staged):
    source, inventory = staged
    (source / "foreign").mkdir()
    (source / "foreign/build.gradle").write_text("plugins {}")
    execution.capture_release_inventory(source, inventory)
    (source / "foreign/build").mkdir()
    (source / "foreign/build/source.java").write_text("unexpected source")
    with pytest.raises(ValueError, match="source"):
        execution.verify_release_source(
            inventory_file=inventory,
            provider=LocalProvider(source.parent),
            request=object(),
            source_dir=ROOT + "/source",
        )


@pytest.mark.nanofaas
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_recipe_log_does_not_preclaim_producer_output(
    staged, tmp_path, monkeypatch, architecture
):
    original = RecipeExecutor.run

    def run_with_output_claim(self, task, *, dry_run=False):
        group = next(g for g in self.provider.groups if g.name in task.argv[-1])
        output = (
            self.provider.root
            / f"recipe-output/{self.provider.architecture}"
            / group.flavor
        )
        # Exercise real shell redirection before the producer claims an empty
        # output. NanoFaaS cleanRecipe refuses nonempty, unowned directories.
        claim = shlex.join(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "assert not list(Path(sys.argv[1]).iterdir()), "
                "'unowned recipe output is not empty'; print('producer log')",
                str(output),
            )
        )
        script = task.argv[-1]
        producer = script[2 : script.index("; } >")]
        result = subprocess.run(
            ("sh", "-c", self.provider.path(script.replace(producer, claim, 1))),
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return original(self, task, dry_run=dry_run)

    monkeypatch.setattr(RecipeExecutor, "run", run_with_output_claim)
    evidence, _, _ = _run(staged, tmp_path, architecture=architecture)
    assert len([e for e in evidence if e.reference.endswith("gradle.log")]) == 3


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    ("architecture", "role"), [("amd64", "stack"), ("arm64", "arm-builder")]
)
def test_recipe_commands_bind_architecture_role_and_paths(architecture, role):
    groups = _groups(architecture=architecture)
    commands = execution.release_recipe_commands(
        groups,
        source_dir=ROOT + "/source",
        remote_root=ROOT,
        builder_name="owned",
        architecture=architecture,
        role=role,
    )
    assert {command.role for command in commands} == {role}
    for command, group in zip(commands, groups, strict=True):
        assert command.task_id == f"release.{architecture}.recipe.{group.flavor}"
        assert (
            f"-Precipe={ROOT}/recipe-inputs/{architecture}/{group.name}.yaml"
            in command.argv
        )
        assert (
            f"-PrecipeOutput={ROOT}/recipe-output/{architecture}/{group.flavor}"
            in command.argv
        )
        assert f"-PrecipeTag={group.tag}" in command.argv
        assert command.options.remote_dir == ROOT + "/source"
        assert command.options.env == {
            "DOCKER_BUILDKIT": "1",
            "BUILDX_BUILDER": "owned",
        }


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    ("architecture", "role", "mixed"),
    [
        ("amd64", "arm-builder", False),
        ("arm64", "stack", False),
        ("arm64", "loadgen", False),
        ("amd64", "stack", True),
        ("arm64", "arm-builder", True),
        ("ppc64", "stack", False),
    ],
)
def test_recipe_wrong_architecture_or_role_fails_before_remote_work(
    tmp_path, architecture, role, mixed
):
    groups = _groups(architecture="arm64" if architecture == "arm64" else "amd64")
    if mixed:
        other = _groups(architecture="amd64" if architecture == "arm64" else "arm64")
        groups = (groups[0], other[1], groups[2])
    provider = LocalProvider(tmp_path)
    executor = RecipeExecutor(provider)
    with pytest.raises(ValueError, match=r"architecture|role|cells"):
        execution.run_release_recipe_steps(
            cast(TaskInputs, object()),
            groups=groups,
            executor=cast(RoleBoundCommandTaskExecutor, executor),
            provider=provider,
            request=object(),
            source_dir=ROOT + "/source",
            remote_root=ROOT,
            evidence_dir=tmp_path / "evidence",
            inventory_file=tmp_path / "absent.json",
            source_commit="a" * 40,
            archive_digest="sha256:" + "b" * 64,
            builder_name="owned",
            architecture=architecture,
            role=role,
        )
    assert not provider.commands and not executor.commands
    assert not (tmp_path / "evidence").exists()


@pytest.mark.nanofaas
@pytest.mark.parametrize("host", ["aarch64", "arm64"])
def test_arm_recipe_reports_match_independent_daemon(staged, tmp_path, host):
    evidence, provider, executor = _run(
        staged, tmp_path, architecture="arm64", host=host
    )
    images = [e for e in evidence if e.kind == "local-image-digest"]
    assert len(images) == len({e.reference for e in images}) == 48
    assert all("-arm64" in e.reference for e in images)
    assert {c.role for c in executor.commands} == {"arm-builder"}
    assert len([e for e in evidence if e.reference.endswith("distribution.json")]) == 3
    assert len([e for e in evidence if e.reference.endswith("gradle.log")]) == 3
    facts = json.loads((tmp_path / "evidence/build-facts.json").read_text())
    assert facts["host"] == host and facts["hostOS"] == "Linux"
    assert facts["architecture"] == "arm64" and facts["role"] == "arm-builder"
    assert ("uname", "-s") in provider.commands


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    "options",
    [
        {"host": "x86_64"},
        {"host_os": "Darwin"},
        {"daemon": "windows|arm64"},
        {"daemon": "linux|amd64"},
        {"image_override": "linux|amd64|sha256:" + "1" * 64},
        {"image_override": "windows|arm64|sha256:" + "1" * 64},
        {"image_override": "linux|arm64|sha256:" + "f" * 64},
    ],
)
def test_arm_native_platform_disagreement_prevents_evidence(staged, tmp_path, options):
    with pytest.raises(ValueError, match=r"native|image|platform"):
        _run(staged, tmp_path, architecture="arm64", **options)


@pytest.mark.nanofaas
def test_arm_final_union_detects_earlier_tag_replacement(staged, tmp_path):
    with pytest.raises(ValueError, match="Final release image"):
        _run(staged, tmp_path, architecture="arm64", replace_after_default=True)


@pytest.mark.nanofaas
def test_arm_cleanup_preserves_amd64_inputs_and_diagnostics(tmp_path):
    root = tmp_path / "vm"
    root.mkdir()
    provider = LocalProvider(root, "arm64")
    kept = [
        root / "recipe-inputs/amd64/keep",
        root / "recipe-output/amd64/keep",
        tmp_path / "recipe-evidence/amd64/keep",
        tmp_path / "recipe-evidence/arm64/gradle.log",
    ]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("retained")
    resource = release_recipe_inputs_resource(
        groups=provider.groups,
        max_parallelism=2,
        run_dir=tmp_path / "recipe-inputs/arm64",
        remote_root=ROOT,
        provider=provider,
        request=object(),
        architecture="arm64",
    )
    value = resource.acquire(cast(TaskInputs, object()))
    assert value.name == "buildkitd-arm64.toml"
    (root / "recipe-output/arm64/native").mkdir(parents=True)
    resource.release(cast(TaskInputs, object()), value)
    assert not (root / "recipe-inputs/arm64").exists()
    assert not (root / "recipe-output/arm64").exists()
    assert all(path.read_text() == "retained" for path in kept)


@pytest.mark.nanofaas
@pytest.mark.parametrize("deleted", [False, True])
def test_arm_reacquisition_does_not_repair_retained_evidence(tmp_path, deleted):
    root = tmp_path / "vm"
    root.mkdir()
    provider = LocalProvider(root, "arm64")
    retained = tmp_path / "recipe-evidence/arm64/release-arm64-jvm.yaml"
    retained.parent.mkdir(parents=True)
    if not deleted:
        retained.write_text("tampered")
    resource = release_recipe_inputs_resource(
        groups=provider.groups,
        max_parallelism=2,
        run_dir=tmp_path / "recipe-inputs/arm64",
        remote_root=ROOT,
        provider=provider,
        request=object(),
        architecture="arm64",
    )
    for _ in range(2):
        value = resource.acquire(cast(TaskInputs, object()))
        resource.release(cast(TaskInputs, object()), value)
    assert not retained.exists() if deleted else retained.read_text() == "tampered"


@pytest.mark.nanofaas
def test_arm_cancellation_after_first_group_retains_diagnostics(
    staged, tmp_path, monkeypatch
):
    from dataclasses import dataclass

    from sonata_engine import Task, TaskOutcome, Workflow

    source, _inventory = staged
    provider = LocalProvider(source.parent, "arm64")
    amd = source.parent / "recipe-output/amd64/keep"
    amd.parent.mkdir(parents=True)
    amd.write_text("unrelated AMD output")
    resource = release_recipe_inputs_resource(
        groups=provider.groups,
        max_parallelism=2,
        run_dir=tmp_path / "inputs/arm64",
        remote_root=ROOT,
        provider=provider,
        request=object(),
        architecture="arm64",
    )
    original = RecipeExecutor.run

    def cancel_native(self, task, **kwargs):
        if ".native" in task.task_id:
            path = self.provider.root / "recipe-output/arm64/logs/native.log"
            path.write_text("interrupted native build\n")
            raise KeyboardInterrupt("cancelled native")
        return original(self, task, **kwargs)

    monkeypatch.setattr(RecipeExecutor, "run", cancel_native)

    @dataclass
    class Assemble(Task[None]):
        title: str = "Assemble ARM recipes"

        def run(self, inputs):
            inputs.resource(resource)
            _run(staged, tmp_path, architecture="arm64")
            return TaskOutcome()

    workflow = Workflow("arm-cancellation")
    workflow.add(Assemble(), requires=(resource,))
    with pytest.raises(KeyboardInterrupt, match="cancelled native"):
        workflow.run()
    assert (tmp_path / "evidence/jvm/distribution.json").is_file()
    assert (
        tmp_path / "evidence/native/gradle.log"
    ).read_text() == "interrupted native build\n"
    assert not (tmp_path / "evidence/native/distribution.json").exists()
    assert not (source.parent / "recipe-inputs/arm64").exists()
    assert not (source.parent / "recipe-output/arm64").exists()
    assert amd.read_text() == "unrelated AMD output"


def _both_recipe_workflow(tmp_path):
    """Real Sonata/files/reports; substitute the two external daemons and registry."""
    from sonata_engine import Evidence, JournalConfig, Workflow
    from sonata_tasks.tasks.models import TaskResult

    from nanolab.plans.release_phases import (
        build_recipe_assembly_phase,
        build_registry_push_phase,
    )
    from nanolab.release.evidence import release_evidence_verifiers
    from nanolab.release.model import digest_path
    from nanolab.release.tasks import (
        arm64_smoke_task,
        benchmark_task,
        publish_architectures_task,
        registry_artifacts_from_receipt,
        regression_gate_task,
        require_release_barriers,
    )

    prepared = {}
    registry = {}
    counts = {
        "amd64-build": 0,
        "arm64-build": 0,
        "amd64-push": 0,
        "arm64-push": 0,
        "benchmark": 0,
        "smoke": 0,
        "publish": 0,
    }
    for architecture in ("amd64", "arm64"):
        root = tmp_path / architecture
        source = root / "vm/source"
        source.mkdir(parents=True)
        (source / "settings.gradle").write_text('rootProject.name="probe"\n')
        (source / "build.gradle").write_text("plugins {}\n")
        inventory = execution.capture_release_inventory(source, root / "inventory.json")
        phase, provider, producer, prerequisite, args = _recipe_phase(
            (source, inventory), root, architecture=architecture
        )
        prepared[architecture] = {
            "phase": phase,
            "provider": provider,
            "producer": producer,
            "source": prerequisite,
            "args": args,
        }

    class Executor:
        def binding_key(self, role):
            return "two-daemons:" + role

        def run(self, task, **kwargs):
            architecture = "arm64" if task.role == "arm-builder" else "amd64"
            item = prepared[architecture]
            provider = item["provider"]
            if task.task_id.startswith("release.") and ".recipe." in task.task_id:
                provider.replace_earlier_tag = False
                counts[architecture + "-build"] += 1
                return item["producer"].run(task, **kwargs)
            if task.argv[:3] == ("docker", "image", "inspect"):
                result = provider.exec_argv(object(), task.argv)
                return TaskResult("", "passed", 0, stdout=result.stdout)
            if task.argv[:2] == ("docker", "push"):
                counts[architecture + "-push"] += 1
                registry[task.argv[-1]] = (
                    "sha256:" + hashlib.sha256(task.argv[-1].encode()).hexdigest()
                )
                return TaskResult("", "passed", 0)
            if task.argv[:2] == ("skopeo", "inspect"):
                return TaskResult(
                    "",
                    "passed",
                    0,
                    stdout=registry[task.argv[-1].removeprefix("docker://")],
                )
            assert task.role == "stack" and task.argv[:2] == ("sh", "-c")
            return TaskResult(
                "",
                "passed",
                0,
                stdout="\n".join(registry[image] for image in task.argv[4:]),
            )

    class Provider:
        def exec_argv(self, request, argv, **kwargs):
            if argv[:2] == ("skopeo", "inspect"):
                assert request == "stack"
                return SimpleNamespace(
                    return_code=0,
                    stdout=registry[argv[-1].removeprefix("docker://")],
                    stderr="",
                )
            architecture = "arm64" if request == "arm-builder" else "amd64"
            return prepared[architecture]["provider"].exec_argv(request, argv, **kwargs)

    executor = cast(RoleBoundCommandTaskExecutor, Executor())
    identity = prepared["amd64"]["phase"].identity
    run_dir = tmp_path / "run"
    gate_file = tmp_path / "gate-decision.json"
    smoke_file = tmp_path / "arm-smoke.json"

    def gate_work(_inputs):
        gate_file.write_text('{"passed": true}')
        return (Evidence("file-digest", str(gate_file), digest_path(gate_file)),)

    gate = regression_gate_task(
        identity=identity, run_dir=run_dir, phase_inputs={}, work=gate_work
    )
    for architecture in ("amd64", "arm64"):
        item = prepared[architecture]
        args = item["args"] | {"executor": executor}
        if architecture == "arm64":
            args["prerequisite_phases"] = (item["source"], gate)
        images, build = build_recipe_assembly_phase(**args)
        push = build_registry_push_phase(
            identity=identity,
            run_dir=run_dir,
            image_plan=args["image_plan"],
            release_images=images,
            executor=executor,
            prerequisite_phases=(),
            assembly=build,
            architecture=architecture,
            role="stack" if architecture == "amd64" else "arm-builder",
        )
        item.update(build=build, push=push)

    benchmarks = []
    for index in range(1, 4):
        report = tmp_path / f"benchmark-{index}.json"

        def benchmark_work(_inputs, report=report):
            counts["benchmark"] += 1
            report.write_text('{"measured": true}')
            return (Evidence("file-digest", str(report), digest_path(report)),)

        benchmarks.append(
            benchmark_task(
                index,
                identity=identity,
                run_dir=run_dir,
                phase_inputs={},
                prerequisites=(prepared["amd64"]["push"].receipt,),
                work=benchmark_work,
            )
        )
    from dataclasses import replace

    gate = replace(gate, prerequisites=tuple(phase.receipt for phase in benchmarks))

    arm = prepared["arm64"]

    def smoke_work(_inputs):
        artifacts = registry_artifacts_from_receipt(
            arm["push"].receipt, arm["build"].expected_images
        )
        counts["smoke"] += 1
        smoke_file.write_text(
            json.dumps(
                {
                    "architecture": "linux/arm64",
                    "images": {
                        a.reference.removeprefix("docker://"): a.digest
                        for a in artifacts
                    },
                }
            )
        )
        return (Evidence("file-digest", str(smoke_file), digest_path(smoke_file)),)

    smoke = arm64_smoke_task(
        identity=identity,
        run_dir=run_dir,
        phase_inputs={},
        prerequisites=(arm["push"].receipt,),
        work=smoke_work,
    )

    def publish_work(_inputs):
        require_release_barriers(
            gate_receipt=gate.receipt,
            gate_file=gate_file,
            smoke_receipt=smoke.receipt,
            smoke_file=smoke_file,
            arm_push_receipt=arm["push"].receipt,
            arm_images=arm["build"].expected_images,
        )
        counts["publish"] += 1
        report = tmp_path / "publication.json"
        report.write_text('{"fixture": true}')
        return (Evidence("file-digest", str(report), digest_path(report)),)

    publication = publish_architectures_task(
        identity=identity,
        run_dir=run_dir,
        phase_inputs={},
        prerequisites=(gate.receipt, smoke.receipt, arm["push"].receipt),
        work=publish_work,
    )
    workflow = Workflow("both-recipes-resume")
    for phase in (
        prepared["amd64"]["build"],
        prepared["amd64"]["push"],
        *benchmarks,
        gate,
        arm["build"],
        arm["push"],
        smoke,
        publication,
    ):
        requires = ()
        for architecture in ("amd64", "arm64"):
            item = prepared[architecture]
            if phase is item["build"]:
                resource = release_recipe_inputs_resource(
                    groups=item["provider"].groups,
                    max_parallelism=2,
                    run_dir=item["args"]["run_dir"]
                    / f"releases/9.9.9/recipe-inputs/{architecture}",
                    remote_root=ROOT,
                    provider=item["provider"],
                    request=object(),
                    architecture=architecture,
                )
                requires = (resource,)
        workflow.add(phase, requires=requires)
    mapping = {
        "docker-daemon:" + image: role
        for architecture, role in (("amd64", "stack"), ("arm64", "arm-builder"))
        for image in prepared[architecture]["build"].expected_images
    }
    verifiers = release_evidence_verifiers(
        Provider(), "stack", local_image_requests=mapping
    )
    return {
        "workflow": workflow,
        "journal": JournalConfig(tmp_path / "journal.jsonl"),
        "verifiers": verifiers,
        "prepared": prepared,
        "counts": counts,
        "smoke": smoke,
        "smoke_file": smoke_file,
        "publication": publication,
    }


def _run_both_recipes(case, *, resume=False):
    case["workflow"].run(
        journal=case["journal"], verifiers=case["verifiers"], resume=resume
    )


@pytest.mark.nanofaas
def test_both_architecture_resume_performs_no_build_or_push(tmp_path):
    case = _both_recipe_workflow(tmp_path)
    _run_both_recipes(case)
    initial = dict(case["counts"])
    assert initial == {
        "amd64-build": 3,
        "arm64-build": 3,
        "amd64-push": 48,
        "arm64-push": 48,
        "benchmark": 3,
        "smoke": 1,
        "publish": 1,
    }
    _run_both_recipes(case, resume=True)
    assert case["counts"] == initial


@pytest.mark.nanofaas
@pytest.mark.parametrize(
    "mutation",
    [
        "report-delete",
        "report-tamper",
        "profile-delete",
        "profile-tamper",
        "config-delete",
        "config-tamper",
        "inventory-delete",
        "inventory-tamper",
        "receipt",
        "image-id",
    ],
)
def test_arm_evidence_invalidation_preserves_verified_amd64_phase(tmp_path, mutation):
    case = _both_recipe_workflow(tmp_path)
    _run_both_recipes(case)
    arm = case["prepared"]["arm64"]
    evidence = arm["args"]["run_dir"] / "releases/9.9.9/recipe-evidence/arm64"
    filenames = {
        "report": "jvm/distribution.json",
        "profile": "release-arm64-jvm.yaml",
        "config": "buildkitd-arm64.toml",
        "inventory": "source-inventory.json",
    }
    if mutation == "receipt":
        arm["build"].receipt.write_text("{}")
    elif mutation == "image-id":
        arm["provider"].replace_earlier_tag = True
    else:
        key, action = mutation.split("-")
        path = evidence / filenames[key]
        if action == "delete":
            path.unlink()
        else:
            path.write_text("tampered")
    _run_both_recipes(case, resume=True)
    assert case["counts"] == {
        "amd64-build": 3,
        "arm64-build": 6,
        "amd64-push": 48,
        "arm64-push": 96,
        "benchmark": 3,
        "smoke": 2,
        "publish": 2,
    }


@pytest.mark.nanofaas
def test_arm_failed_resume_invalidates_prior_receipts_and_preserves_amd64(tmp_path):
    case = _both_recipe_workflow(tmp_path)
    _run_both_recipes(case)
    arm = case["prepared"]["arm64"]
    retained = (
        arm["args"]["run_dir"]
        / "releases/9.9.9/recipe-evidence/arm64/jvm/distribution.json"
    )
    retained.unlink()
    arm["provider"].fail_group = "native"
    with pytest.raises(RuntimeError, match="assembly failed"):
        _run_both_recipes(case, resume=True)
    assert not arm["build"].receipt.exists()
    assert case["counts"]["amd64-build"] == 3 and case["counts"]["amd64-push"] == 48
    assert (
        case["counts"]["arm64-push"] == 48
        and case["counts"]["smoke"] == 1
        and case["counts"]["publish"] == 1
    )
    assert (retained.parent.parent / "native/gradle.log").is_file()


@pytest.mark.nanofaas
def test_legacy_arm_journal_requires_recipe_assembly_and_new_push(tmp_path):
    from dataclasses import replace

    from sonata_engine import Evidence, JournalConfig, Workflow
    from sonata_engine.errors import WorkflowTopologyMismatchError

    case = _both_recipe_workflow(tmp_path)
    arm = case["prepared"]["arm64"]
    # Genuine journal record for the old combined contract, under the same phase ID.
    legacy = replace(
        arm["build"],
        phase_inputs={"images": arm["build"].expected_images, "source": "a" * 40},
        prerequisites=(arm["source"].receipt,),
        work=lambda _inputs: tuple(
            Evidence("local-registry-digest", "docker://" + image, "sha256:" + "e" * 64)
            for image in arm["build"].expected_images
        ),
    )
    old = Workflow("both-recipes-resume")
    old.add(legacy)
    old.run(journal=case["journal"], verifiers=case["verifiers"])
    original_journal = case["journal"].path.read_bytes()
    with pytest.raises(WorkflowTopologyMismatchError):
        _run_both_recipes(case, resume=True)
    assert case["journal"].path.read_bytes() == original_journal
    assert case["counts"]["arm64-build"] == 0 and case["counts"]["arm64-push"] == 0
    case["journal"] = JournalConfig(tmp_path / "recipe-contract-2.jsonl")
    _run_both_recipes(case)
    assert case["counts"]["arm64-build"] == 3 and case["counts"]["arm64-push"] == 48
    assert case["counts"]["smoke"] == 1 and case["counts"]["publish"] == 1


@pytest.mark.nanofaas
def test_stale_arm_smoke_cannot_publish(tmp_path):
    from sonata_engine import Selection

    from nanolab.release.model import digest_path

    case = _both_recipe_workflow(tmp_path)
    _run_both_recipes(case)
    smoke_file, smoke = case["smoke_file"], case["smoke"]
    data = json.loads(smoke_file.read_text())
    first = next(iter(data["images"]))
    data["images"][first] = "sha256:" + "f" * 64
    smoke_file.write_text(json.dumps(data))
    payload = json.loads(smoke.receipt.read_text())
    next(entry for entry in payload["evidence"] if entry["kind"] == "file-digest")[
        "digest"
    ] = digest_path(smoke_file)
    smoke.receipt.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="does not match"):
        case["workflow"].run(select=Selection(only="publish-architecture-images"))
    assert case["counts"]["publish"] == 1
    assert not case["publication"].receipt.exists()
