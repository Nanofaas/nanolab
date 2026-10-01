"""Archive changes, incomplete transfers and stale tags must stop assembly evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from nanolab.images.plan import build_image_plan
from nanolab.release import recipe_execution as execution
from nanolab.release.resources import release_recipe_inputs_resource

from .test_recipe import _groups, _report

ROOT = "/home/azureuser/nanofaas-release/v9.9.9"


class LocalProvider:
    """Run transport commands on real files; substitute only the VM root."""

    def __init__(self, root: Path):
        self.root = root
        self.groups = _groups()
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
                        "linux|amd64|sha256:" + "f" * 64
                        if self.replace_earlier_tag and ref.endswith("-jvm")
                        else f"linux|amd64|{ids[ref]}"
                    )
                    for ref in refs
                ),
            )
        if argv[0] in {"uname", "docker"}:
            return SimpleNamespace(
                return_code=0,
                stderr="",
                stdout=(
                    "x86_64\n"
                    if argv[0] == "uname"
                    else "linux|amd64\n"
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
        output = self.provider.root / "recipe-output" / "amd64" / group.flavor
        assert not (output / "distribution.json").exists()
        output.mkdir(parents=True, exist_ok=True)
        (output / "gradle.log").write_text("complete Gradle log\n")
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
def test_archive_inventory_rejects_source_mutation(staged, mutation):
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
            provider=LocalProvider(source.parent),
            request=object(),
            source_dir=ROOT + "/source",
        )


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
):
    source, inventory = staged
    archive_bytes = b"verified archive"
    (source.parent / "source.tar").write_bytes(
        b"changed" if corrupt_archive else archive_bytes
    )
    provider = LocalProvider(source.parent)
    provider.image_override, provider.fail_group, provider.truncate = (
        image_override,
        fail_group,
        truncate,
    )
    provider.replace_after_default = replace_after_default
    executor = RecipeExecutor(provider)
    local = tmp_path / "evidence"
    local.mkdir(exist_ok=True)
    config = local / "buildkitd-amd64.toml"
    config.write_text("[worker.oci]\n  max-parallelism = 2\n")
    remote = source.parent / "recipe-inputs/amd64"
    remote.mkdir(parents=True)
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
    )
    return result, provider, executor


def test_complete_reports_match_independent_daemon_inspection(staged, tmp_path):
    evidence, provider, executor = _run(staged, tmp_path)
    images = [e for e in evidence if e.kind == "local-image-digest"]
    assert len(images) == 44
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


def test_failed_group_cannot_reuse_stale_output(staged, tmp_path):
    stale = staged[0].parent / "recipe-output/amd64/native"
    stale.mkdir(parents=True)
    (stale / "distribution.json").write_text("old successful report")
    with pytest.raises(RuntimeError, match="assembly failed"):
        _run(staged, tmp_path, fail_group="native")
    assert not (stale / "distribution.json").exists()
    assert (tmp_path / "evidence/jvm/distribution.json").exists()
    assert (
        tmp_path / "evidence/native/gradle.log"
    ).read_text() == "complete Gradle log\n"
    assert not (tmp_path / "evidence/native/distribution.json").exists()


def test_transfer_truncation_prevents_receipt(staged, tmp_path):
    with pytest.raises(ValueError, match=r"transfer|digest|size"):
        _run(staged, tmp_path, truncate=True)


@pytest.mark.parametrize("failure", ["transfer", "cancel", "cleanup"])
def test_partial_recipe_failure_compensates_owned_inputs(tmp_path, failure):
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
    provider = FailingProvider(root)
    provider.fail_cleanup = failure == "cleanup"
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
        assert not (root / "recipe-inputs/amd64").exists()
    assert (unrelated / "keep").read_text() == "keep"
    assert (tmp_path / "evidence/release-amd64-jvm.yaml").exists()
    assert (tmp_path / "evidence/buildkitd-amd64.toml").exists()


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


@pytest.mark.parametrize("which", ["archive", "config"])
def test_acquired_input_digest_mismatch_prevents_assembly(staged, tmp_path, which):
    with pytest.raises(ValueError, match=r"archive|config"):
        _run(
            staged,
            tmp_path,
            corrupt_archive=which == "archive",
            corrupt_config=which == "config",
        )


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


def test_final_inspection_rejects_earlier_tag_replaced_by_later_group(staged, tmp_path):
    with pytest.raises(ValueError, match="image"):
        _run(staged, tmp_path, replace_after_default=True)


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


def _recipe_phase(staged, tmp_path, **overrides):
    from nanolab.plans.release_phases import build_amd64_phase
    from nanolab.release.model import ReleaseIdentity, digest_path
    from nanolab.release.tasks import source_test_task

    source, inventory = staged
    provider = LocalProvider(source.parent)
    archive = source.parent / "source.tar"
    archive.write_bytes(b"guarded source")
    run_dir = tmp_path / "release-run"
    release_dir = run_dir / "releases/9.9.9"
    inputs = release_dir / "recipe-inputs/amd64"
    inputs.mkdir(parents=True)
    remote = source.parent / "recipe-inputs/amd64"
    remote.mkdir(parents=True)
    for g in provider.groups:
        for directory in (inputs, remote):
            (directory / f"{g.name}.yaml").write_bytes(g.profile_bytes)
    for directory in (inputs, remote):
        (directory / "buildkitd-amd64.toml").write_text(
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
        "identity": identity,
        "run_dir": run_dir,
        "image_plan": build_image_plan(
            Path(os.environ["NANOFAAS_ROOT"]), "v9.9.9", architectures=("amd64",)
        ),
        "max_parallelism": 2,
        "builder_name": "owned-builder",
        "remote_root": ROOT,
        "source_dir": ROOT + "/source",
        "executor": cast(RoleBoundCommandTaskExecutor, executor),
        "source_tests": source_tests,
        "recipe_groups": provider.groups,
        "provider": provider,
        "request": object(),
        "inventory_file": inventory,
        "archive_digest": digest_path(archive),
    }
    arguments.update(overrides)
    _, phase = build_amd64_phase(**arguments)
    return phase, provider, executor, source_tests, arguments


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
def test_owned_builder_preserves_previous_selection(fail_create):
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
        role="stack",
        driver_options=("default-load=true",),
    )
    owned = resources.preserve_release_builder_selection(
        original, executor=executor, name="owned"
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
        _, other = release_phases.build_amd64_phase(**args)
    assert other.reuse_key != original_key


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
