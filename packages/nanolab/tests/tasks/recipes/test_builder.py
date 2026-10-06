from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult

REGISTRATION = "enabled\ninterpreter /usr/bin/qemu-x86_64\nflags: F\n"


class BuilderExecutor:
    def __init__(self, *, registration: str = "", failure: str | None = None):
        self.registration = registration
        self.failure = failure
        self.commands: list[tuple[str, ...]] = []
        self.builder = ""
        self.node = ""

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        argv = task.argv
        self.commands.append(argv)
        stdout = ""
        failed = False
        if argv[1] == "info":
            stdout = json.dumps(
                {"id": "test-daemon", "os": "linux", "architecture": "aarch64"}
            )
        elif argv[1:3] == ("buildx", "ls"):
            stdout = "nanolab-heap-analysis\n" + (
                self.builder + "\n"
                if self.builder
                else "nanolab-recipe-test\n"
                if self.failure == "collision"
                else ""
            )
        elif argv[1:3] == ("buildx", "create"):
            failed = self.failure == "create"
            if not failed:
                self.builder = argv[argv.index("--name") + 1]
                self.node = (
                    argv[argv.index("--node") + 1]
                    if "--node" in argv
                    else "default-node"
                )
                stdout = self.builder
                if self.failure == "cancel-after-create":
                    raise KeyboardInterrupt("cancelled after creation")
        elif argv[1:3] == ("buildx", "inspect"):
            failed = self.failure == "bootstrap" and "--bootstrap" in argv
            if not self.builder:
                return TaskResult(
                    task_id="",
                    status="failed",
                    return_code=1,
                    stdout="",
                    stderr="no builder found",
                )
            stdout = (
                f"Name: {self.builder}\nDriver: docker-container\nNodes:\n"
                f"Name: {self.node}\n"
                "Platforms: linux/amd64, linux/arm64\n"
            )
        elif argv[1:3] == ("buildx", "rm"):
            self.builder = ""
        elif "--install" in argv:
            assert argv[-2:] == ("--install", "amd64")
            self.registration = REGISTRATION
            failed = self.failure == "install"
        elif "--platform" in argv:
            failed = self.failure == "probe"
            if self.failure == "cancel":
                raise KeyboardInterrupt("cancelled")
        elif argv[-1] == "inspect-registration":
            stdout = self.registration
        elif argv[-2] == "remove-registration":
            if self.registration != argv[-1].rstrip("\n") and self.registration.rstrip(
                "\n"
            ) != argv[-1].rstrip("\n"):
                failed = True
            else:
                self.registration = ""
        else:
            raise AssertionError(argv)
        return TaskResult(
            task_id="",
            status="failed" if failed else "passed",
            return_code=1 if failed else 0,
            stdout=stdout,
            stderr="forced failure" if failed else "",
        )


def resource(tmp_path: Path, executor: BuilderExecutor, *, tag="recipe-test"):
    from nanolab.tasks.recipes.builder import recipe_builder_resource

    return recipe_builder_resource(executor=executor, run_dir=tmp_path, tag=tag)


def test_builder_uses_daemon_architecture_and_explicit_name(tmp_path: Path) -> None:
    executor = BuilderExecutor()
    builder = resource(tmp_path, executor)
    value = builder.acquire(TaskInputs._for_resources({}, set()))
    assert value.platform == "linux/arm64"
    assert value.name == "nanolab-recipe-test"
    create = next(
        argv for argv in executor.commands if argv[1:3] == ("buildx", "create")
    )
    assert "network=host" in create
    assert "docker-container" in create
    assert all("--use" not in argv for argv in executor.commands)
    config = Path(create[create.index("--buildkitd-config") + 1]).read_text()
    assert '[registry."127.0.0.1:5000"]' in config
    builder.release(TaskInputs._for_resources({}, set()), value)
    assert executor.builder == ""
    assert executor.registration == ""


def test_probe_and_helper_use_distinct_platform_manifest_digests(
    tmp_path: Path,
) -> None:
    executor = BuilderExecutor()
    builder = resource(tmp_path, executor)
    value = builder.acquire(TaskInputs._for_resources({}, set()))
    try:
        helper = next(
            argv for argv in executor.commands if argv[-1] == "inspect-registration"
        )
        probe = next(argv for argv in executor.commands if "--platform" in argv)
        helper_image = helper[helper.index("--privileged") + 1]
        probe_image = probe[probe.index("--platform") + 2]
        assert helper_image != probe_image
        assert "@sha256:" in helper_image and "@sha256:" in probe_image
    finally:
        builder.release(TaskInputs._for_resources({}, set()), value)


def test_existing_foreign_registration_is_preserved(tmp_path: Path) -> None:
    executor = BuilderExecutor(registration=REGISTRATION)
    builder = resource(tmp_path, executor)
    value = builder.acquire(TaskInputs._for_resources({}, set()))
    builder.release(TaskInputs._for_resources({}, set()), value)
    assert executor.registration == REGISTRATION
    assert not any(
        "--install" in argv or "remove-registration" in argv
        for argv in executor.commands
    )


@pytest.mark.parametrize("changed", [False, True])
def test_owned_registration_is_removed_only_when_unchanged(
    tmp_path: Path, changed: bool
) -> None:
    executor = BuilderExecutor()
    builder = resource(tmp_path, executor)
    value = builder.acquire(TaskInputs._for_resources({}, set()))
    if changed:
        executor.registration = "changed by another tool"
        with pytest.raises(RuntimeError, match=r"cleanup|registration"):
            builder.release(TaskInputs._for_resources({}, set()), value)
        assert executor.registration == "changed by another tool"
        assert "conflict" in (tmp_path / "builder-cleanup.json").read_text()
    else:
        builder.release(TaskInputs._for_resources({}, set()), value)
        assert executor.registration == ""


@pytest.mark.parametrize(
    "failure", ["install", "probe", "create", "bootstrap", "cancel"]
)
def test_builder_compensates_partial_acquisition(tmp_path: Path, failure: str) -> None:
    executor = BuilderExecutor(failure=failure)
    builder = resource(tmp_path, executor)
    with pytest.raises((RuntimeError, KeyboardInterrupt), match=r"failure|cancel"):
        builder.acquire(TaskInputs._for_resources({}, set()))
    assert executor.builder == ""
    assert executor.registration == ""


def test_builder_name_collision_never_removes_existing_builder(tmp_path: Path) -> None:
    executor = BuilderExecutor(failure="collision")
    with pytest.raises(RuntimeError, match=r"exists|collision"):
        resource(tmp_path, executor).acquire(TaskInputs._for_resources({}, set()))
    assert not any(argv[1:3] == ("buildx", "rm") for argv in executor.commands)
    assert not any("--install" in argv for argv in executor.commands)


def test_two_runs_serialize_registration_lifetime(tmp_path: Path) -> None:
    executor = BuilderExecutor()
    first = resource(tmp_path / "one", executor)
    second = resource(tmp_path / "two", executor, tag="recipe-second")
    value = first.acquire(TaskInputs._for_resources({}, set()))
    try:
        with pytest.raises(RuntimeError, match=r"busy|lock"):
            second.acquire(TaskInputs._for_resources({}, set()))
    finally:
        first.release(TaskInputs._for_resources({}, set()), value)
    assert executor.registration == ""


def test_binfmt_lock_is_independent_of_process_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = BuilderExecutor()
    first_dir, second_dir = tmp_path / "temp-one", tmp_path / "temp-two"
    first_dir.mkdir()
    second_dir.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(first_dir))
    first = resource(tmp_path / "one", executor)
    second = resource(tmp_path / "two", executor, tag="recipe-second")
    inputs = TaskInputs._for_resources({}, set())
    value = first.acquire(inputs)
    second_value = None
    try:
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(second_dir))
        with pytest.raises(RuntimeError, match=r"busy|lock"):
            second_value = second.acquire(inputs)
    finally:
        first.release(inputs, value)
        if second_value is not None:
            second.release(inputs, second_value)


def test_cancellation_after_create_reconciles_owned_builder(tmp_path: Path) -> None:
    executor = BuilderExecutor(failure="cancel-after-create")
    with pytest.raises(KeyboardInterrupt, match="after creation"):
        resource(tmp_path, executor).acquire(TaskInputs._for_resources({}, set()))
    assert executor.builder == ""
    assert executor.registration == ""


@pytest.mark.parametrize("failure", ["evidence", "directory"])
def test_evidence_failure_cannot_bypass_compensation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    executor = BuilderExecutor()
    original_write = Path.write_text
    original_mkdir = Path.mkdir

    def write(path, *args, **kwargs):
        if path.name == "builder.json":
            raise OSError("no space for evidence")
        return original_write(path, *args, **kwargs)

    def mkdir(path, *args, **kwargs):
        if path == tmp_path / "run":
            raise OSError("cannot create evidence directory")
        return original_mkdir(path, *args, **kwargs)

    if failure == "evidence":
        monkeypatch.setattr(Path, "write_text", write)
    else:
        monkeypatch.setattr(Path, "mkdir", mkdir)
    run = resource(tmp_path / "run", executor)
    with pytest.raises(OSError, match=r"evidence|space"):
        run.acquire(TaskInputs._for_resources({}, set()))
    assert executor.registration == ""
    assert executor.builder == ""
    monkeypatch.setattr(Path, "write_text", original_write)
    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    retry = resource(tmp_path / "retry", executor)
    value = retry.acquire(TaskInputs._for_resources({}, set()))
    retry.release(TaskInputs._for_resources({}, set()), value)


def test_recipe_builder_obeys_shared_acquisition_failure(tmp_path, monkeypatch):
    from sonata_engine import Resource

    from nanolab.tasks.recipes import builder as module

    def denied(_inputs):
        raise RuntimeError("shared builder acquisition denied")

    monkeypatch.setattr(
        module,
        "buildx_builder_resource",
        lambda **_kwargs: Resource(
            title="Deny shared builder", acquire=denied, release=lambda *_args: None
        ),
        raising=False,
    )
    executor = BuilderExecutor()
    run = resource(tmp_path, executor)
    inputs = TaskInputs.empty()
    try:
        with pytest.raises(RuntimeError, match="shared builder acquisition denied"):
            run.acquire(inputs)
    finally:
        if executor.builder:
            run.release(inputs, module.RecipeBuilder(executor.builder, "linux/arm64"))
    assert executor.registration == ""
    assert all(argv[1:3] != ("buildx", "create") for argv in executor.commands)


def test_unresolved_partial_builder_cleanup_retains_emulation(tmp_path):
    class InterruptedBuilder(BuilderExecutor):
        def run(self, task, *, dry_run=False):
            argv = task.argv
            if argv[1:3] == ("buildx", "create"):
                self.builder = "nanolab-recipe-test"
                self.node = argv[argv.index("--node") + 1]
                self.commands.append(argv)
                raise KeyboardInterrupt("partial creation interrupted")
            if argv[1:3] == ("buildx", "inspect"):
                self.commands.append(argv)
                raise RuntimeError("builder identity unavailable")
            return super().run(task, dry_run=dry_run)

    executor = InterruptedBuilder()
    with pytest.raises(KeyboardInterrupt, match="partial creation") as caught:
        resource(tmp_path, executor).acquire(TaskInputs.empty())
    assert executor.builder == "nanolab-recipe-test"
    assert executor.registration == REGISTRATION
    receipt = json.loads((tmp_path / "builder-cleanup.json").read_text())
    assert receipt["registrationRetained"] is True
    assert any("cleanup" in note for note in caught.value.__notes__)
    assert not any("remove-registration" in argv for argv in executor.commands)


@pytest.mark.parametrize("change", ["replacement", "additional-node", "remove-failure"])
def test_failed_builder_release_retains_registration_and_releases_lock(
    tmp_path, change
):
    class ChangedExecutor(BuilderExecutor):
        def run(self, task, *, dry_run=False):
            if change == "remove-failure" and task.argv[1:3] == ("buildx", "rm"):
                self.commands.append(task.argv)
                raise RuntimeError("builder removal unavailable")
            result = super().run(task, dry_run=dry_run)
            if change == "additional-node" and task.argv[1:3] == ("buildx", "inspect"):
                return TaskResult(
                    task_id="",
                    status="passed",
                    return_code=0,
                    stdout=result.stdout + "Name: foreign-node\n",
                )
            return result

    executor = ChangedExecutor()
    # Introduce additional-node mutation only after successful acquisition.
    original_change = change
    if change == "additional-node":
        change = "unchanged"
    run = resource(tmp_path / "first", executor)
    inputs = TaskInputs.empty()
    value = run.acquire(inputs)
    change = original_change
    if change == "replacement":
        executor.node = "replacement-node"
    with pytest.raises(RuntimeError, match="cleanup"):
        run.release(inputs, value)
    assert executor.builder == value.name
    assert executor.registration == REGISTRATION
    receipt = json.loads((tmp_path / "first" / "builder-cleanup.json").read_text())
    assert receipt["registrationRetained"] is True
    assert not any("remove-registration" in argv for argv in executor.commands)

    # A distinct builder can acquire after cleanup failure: the local lock closed.
    retry_executor = BuilderExecutor(registration=executor.registration)
    retry = resource(tmp_path / "retry", retry_executor, tag="recipe-second")
    retry_value = retry.acquire(inputs)
    retry.release(inputs, retry_value)
    assert executor.builder == value.name
    assert retry_executor.registration == REGISTRATION
    assert executor.registration == REGISTRATION


def test_recipe_keeps_receipt_and_removes_builder_before_registration(tmp_path):
    executor = BuilderExecutor()
    run = resource(tmp_path, executor)
    inputs = TaskInputs.empty()
    value = run.acquire(inputs)
    receipt = json.loads((tmp_path / "builder.json").read_text())
    assert receipt["ownerNode"] == executor.node
    assert receipt["builder"] == value.name
    assert "Platforms: linux/amd64, linux/arm64" in receipt["bootstrap"]
    run.release(inputs, value)
    remove_builder = next(
        i for i, argv in enumerate(executor.commands) if argv[1:3] == ("buildx", "rm")
    )
    remove_registration = next(
        i for i, argv in enumerate(executor.commands) if "remove-registration" in argv
    )
    assert remove_builder < remove_registration


def test_recipe_platform_validation_failure_cleans_builder_and_emulation(tmp_path):
    class MissingPlatform(BuilderExecutor):
        def run(self, task, *, dry_run=False):
            result = super().run(task, dry_run=dry_run)
            if "--bootstrap" in task.argv:
                return TaskResult(
                    task_id="",
                    status="passed",
                    return_code=0,
                    stdout=result.stdout.replace(
                        "Platforms: linux/amd64, linux/arm64", "Platforms: linux/arm64"
                    ),
                )
            return result

    executor = MissingPlatform()
    with pytest.raises(RuntimeError, match="both required platforms"):
        resource(tmp_path, executor).acquire(TaskInputs.empty())
    assert executor.builder == ""
    assert executor.registration == ""


def test_failed_rm_with_missing_client_record_retains_emulation(tmp_path):
    class RemovedRecordExecutor(BuilderExecutor):
        daemon_alive = True

        def run(self, task, *, dry_run=False):
            if task.argv[1:3] == ("buildx", "rm"):
                self.commands.append(task.argv)
                self.builder = ""
                raise RuntimeError("client record removed but daemon stop failed")
            return super().run(task, dry_run=dry_run)

    executor = RemovedRecordExecutor(failure="bootstrap")
    with pytest.raises(RuntimeError, match="forced failure"):
        resource(tmp_path, executor).acquire(TaskInputs.empty())
    assert executor.daemon_alive
    assert executor.builder == ""
    assert executor.registration == REGISTRATION
    receipt = json.loads((tmp_path / "builder-cleanup.json").read_text())
    assert receipt["registrationRetained"] is True
    assert any("unconfirmed" in error for error in receipt["errors"])
    assert not any("remove-registration" in argv for argv in executor.commands)


@pytest.mark.parametrize("cleanup_error", [KeyboardInterrupt, ValueError])
def test_interrupted_compensation_preserves_primary_receipt_and_lock(
    tmp_path, monkeypatch, cleanup_error
):
    from sonata_engine import Resource

    from nanolab.tasks.recipes import builder as module

    def acquire(_inputs):
        raise ValueError("primary acquisition failure")

    def release(_inputs, _value):
        raise cleanup_error("secondary cleanup failure")

    monkeypatch.setattr(
        module,
        "buildx_builder_resource",
        lambda **_kwargs: Resource(
            title="Interrupted", acquire=acquire, release=release
        ),
    )
    executor = BuilderExecutor()
    with pytest.raises(ValueError, match="primary acquisition failure") as caught:
        resource(tmp_path / "first", executor).acquire(TaskInputs.empty())
    assert any("secondary cleanup failure" in note for note in caught.value.__notes__)
    assert executor.registration == REGISTRATION
    receipt = json.loads((tmp_path / "first" / "builder-cleanup.json").read_text())
    assert receipt["registrationRetained"] is True
    assert any("secondary cleanup failure" in error for error in receipt["errors"])

    # The same daemon's local lock was closed even when release was interrupted.
    with pytest.raises(ValueError, match="primary acquisition failure"):
        resource(tmp_path / "retry", executor).acquire(TaskInputs.empty())
