from __future__ import annotations

import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

from nanolab.images.plan import build_image_plan
from nanolab.release import arm
from nanolab.release import build as release_build
from nanolab.release.model import digest_path

from ._release_support import (
    NANOFAAS_ROOT,
    _ArchiveProvider,
    _ArmFailureProvider,
    _plan,
    _ReleaseProvider,
)


def test_source_tests_reuse_gradle_and_uv_and_pin_container_toolchains() -> None:
    commands = release_build.source_test_commands(Path("/srv/nanofaas-source"))

    gradle_cmd = commands[0]
    assert gradle_cmd.task_id == "release.source.gradle"
    assert gradle_cmd.role == "stack"
    assert gradle_cmd.options.remote_dir == "/srv/nanofaas-source"
    assert gradle_cmd.argv[:2] == ("bash", "-c")
    script = gradle_cmd.argv[2]
    assert "./gradlew test" in script
    assert "--no-parallel" in script
    assert "-u KUBECONFIG" in script
    assert "-u NANOFAAS_RUN_K8S_E2E" in script
    assert "-u NANOFAAS_E2E_NAMESPACE" in script
    python_commands = [
        command
        for command in commands
        if command.task_id == "release.source.python-sdk"
    ]
    assert {command.task_id for command in python_commands} == {
        "release.source.python-sdk",
    }
    by_id = {
        command.task_id: shlex.split(command.argv[2].split("exec ", 1)[1])
        for command in python_commands
    }
    assert tuple(by_id["release.source.python-sdk"][-4:]) == (
        "sdks/python/tests",
        "functions/python/word-stats/tests",
        "functions/python/json-transform/tests",
        "functions/python/roman-numeral/tests",
    )
    container_commands = [
        command for command in commands if command.argv[:2] == ("docker", "run")
    ]
    assert {command.task_id for command in container_commands} == {
        "release.source.go",
        "release.source.node",
        "release.source.rust",
        "release.source.bash",
    }
    for command in container_commands:
        assert command.argv[:2] == ("docker", "run")
        mount = command.argv[command.argv.index("-v") + 1]
        assert mount == "/srv/nanofaas-source:/source:ro"
        assert command.argv[command.argv.index("-w") + 1] == "/workspace"
        assert command.argv[-1].startswith("set -eu; cp -a /source/. /workspace && ")
        image = next(value for value in command.argv if "@sha256:" in value)
        assert len(image.rsplit("@sha256:", 1)[1]) == 64
        assert command.options.remote_dir == "/srv/nanofaas-source"


def test_amd64_commands_delegate_all_image_builds_to_root_recipes() -> None:
    from nanolab.release.recipe import prepare_release_recipe_groups
    from nanolab.release.recipe_execution import release_recipe_commands

    plan = build_image_plan(NANOFAAS_ROOT, "v9.9.9", architectures=("amd64",))
    groups = prepare_release_recipe_groups(
        NANOFAAS_ROOT, plan, profiles_root=Path(__file__).parents[2] / "recipes"
    )
    commands = release_recipe_commands(
        groups, source_dir="/remote/source", remote_root="/remote", builder_name="owned"
    )
    assert len(commands) == 3
    assert all(c.argv[:2] == ("./gradlew", "assembleRecipe") for c in commands)
    assert all(c.options.env["BUILDX_BUILDER"] == "owned" for c in commands)
    assert {c.image for group in groups for c in group.cells} == {
        c.image for c in plan.cells
    }


def test_sonata_owned_arm_resources_are_not_recreated_and_every_image_is_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root, plan.version, registry=plan.image_plan.registry
    )
    events: list[str] = []
    provider = _ReleaseProvider(events)

    evidence = release_build._build_arm64_images(
        plan,
        arm_plan,
        tmp_path / "docker-bake-arm64.json",
        provider,
        object(),
        "/release/docker-bake-arm64.json",
        "/release/buildkitd.toml",
        "/release/source",
        registry_upstream="",
        stage_inputs=False,
        manage_resources=False,
    )

    assert len(evidence) == len(arm_plan.cells)
    assert sum(event.startswith("exec:docker push") for event in events) == len(
        arm_plan.cells
    )
    assert not any("buildx create" in event for event in events)
    assert not any("nanofaas-registry-tunnel" in event for event in events)


def _arm64_build_and_smoke(plan, provider, events: list[str]):
    """Drive the two ARM64 phases the Sonata DAG calls, in DAG order."""
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root, plan.version, registry=plan.image_plan.registry
    )
    built = release_build._build_arm64_images(
        plan,
        arm_plan,
        plan.run_dir / "docker-bake-arm64.json",
        provider,
        object(),
        "/release/docker-bake-arm64.json",
        "/release/buildkitd.toml",
        "/release/source",
        registry_upstream="",
        stage_inputs=False,
        manage_resources=False,
    )
    return arm_plan, release_build._smoke_arm64_images(
        plan,
        arm_plan,
        provider,
        object(),
        built,
        registry_upstream="",
        ensure_tunnel=False,
    )


def test_arm64_smoke_health_checks_every_server_and_probes_the_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ReleaseProvider(events)

    arm_plan, evidence = _arm64_build_and_smoke(plan, provider, events)

    marker = json.loads((plan.run_dir / "arm64-smoke.json").read_text(encoding="utf-8"))
    assert [artifact.reference for artifact in evidence] == [
        str(plan.run_dir / "arm64-smoke.json")
    ]
    assert marker["architecture"] == arm.ARM64_PLATFORM
    assert set(marker["images"]) == {cell.image for cell in arm_plan.cells}
    assert marker["serverHealthChecks"] == [
        smoke.cell.image for smoke in arm.server_smoke_specs(arm_plan)
    ]
    assert marker["watchdog"]["image"] == arm.watchdog_cell(arm_plan).image
    # every smoke container is started by digest and torn down again
    assert all(
        "@sha256:" in event for event in events if event.startswith("exec:docker run")
    )
    assert sum(event.startswith("exec:docker rm --force") for event in events) == len(
        marker["serverHealthChecks"]
    )
    # no tunnel is opened: the Sonata DAG owns that resource
    assert not any("nanofaas-registry-tunnel" in event for event in events)


def test_arm64_smoke_refuses_evidence_that_moved_since_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ReleaseProvider(events)
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root, plan.version, registry=plan.image_plan.registry
    )
    built = release_build._build_arm64_images(
        plan,
        arm_plan,
        plan.run_dir / "docker-bake-arm64.json",
        provider,
        object(),
        "/release/docker-bake-arm64.json",
        "/release/buildkitd.toml",
        "/release/source",
        registry_upstream="",
        stage_inputs=False,
        manage_resources=False,
    )
    moved = arm_plan.cells[0].image
    provider.registry_digests[moved] = "sha256:" + "f" * 64

    with pytest.raises(RuntimeError, match="evidence changed before smoke"):
        release_build._smoke_arm64_images(
            plan,
            arm_plan,
            provider,
            object(),
            built,
            registry_upstream="",
            ensure_tunnel=False,
        )

    assert not (plan.run_dir / "arm64-smoke.json").exists()


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("start", "arm server failed"),
        ("health", "arm health failed"),
        # the original failure must survive a cleanup that also fails
        ("health-cleanup", "arm health failed"),
        ("health-cleanup-raises", "arm health failed"),
    ],
)
def test_arm64_smoke_server_failures_still_remove_the_container(
    failure: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, failure)

    with pytest.raises(RuntimeError, match=error):
        _arm64_build_and_smoke(plan, provider, events)

    assert "exec:docker rm --force nanofaas-arm64-smoke-1" in events
    assert not (plan.run_dir / "arm64-smoke.json").exists()


def test_arm64_smoke_cleanup_does_not_hide_a_provider_programming_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenCleanupProvider(_ArmFailureProvider):
        def exec_argv(self, request, argv, **kwargs):
            if argv[:3] == ("docker", "rm", "--force"):
                raise ValueError("bad cleanup contract")
            return super().exec_argv(request, argv, **kwargs)

    plan = _plan(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="bad cleanup contract"):
        _arm64_build_and_smoke(plan, BrokenCleanupProvider([], "health"), [])


def test_arm64_smoke_rejects_a_watchdog_that_fails_the_wrong_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, "watchdog")

    with pytest.raises(RuntimeError, match="exec format error"):
        _arm64_build_and_smoke(plan, provider, events)

    assert not (plan.run_dir / "arm64-smoke.json").exists()


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("bake", "arm bake failed"),
        ("architecture", "image architecture mismatch"),
        ("push", "arm push failed"),
    ],
)
def test_arm64_build_failures_never_produce_evidence(
    failure: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, failure)

    with pytest.raises(RuntimeError, match=error):
        _arm64_build_and_smoke(plan, provider, events)

    rendered = "\n".join(events).lower()
    assert "ghcr.io" not in rendered
    assert "docker login" not in rendered


def test_source_archive_contains_only_the_exact_guarded_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    subprocess.run(
        ("git", "config", "user.email", "release@example.test"), cwd=repo, check=True
    )
    subprocess.run(("git", "config", "user.name", "Release Test"), cwd=repo, check=True)
    (repo / "tracked.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repo, check=True)
    subprocess.run(("git", "commit", "-qm", "source"), cwd=repo, check=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = tmp_path / "source.tar"

    evidence = release_build.create_source_archive(repo, commit, archive)

    assert evidence.reference == str(archive)
    assert evidence.digest == digest_path(archive)
    with tarfile.open(archive) as source:
        assert source.getnames() == ["tracked.txt"]


def test_source_archive_rechecks_clean_commit_and_never_overwrites_on_guard_failure(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    subprocess.run(
        ("git", "config", "user.email", "release@example.test"), cwd=repo, check=True
    )
    subprocess.run(("git", "config", "user.name", "Release Test"), cwd=repo, check=True)
    (repo / "tracked.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repo, check=True)
    subprocess.run(("git", "commit", "-qm", "source"), cwd=repo, check=True)
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"owned")
    (repo / "untracked-secret").write_text("must-not-enter-archive", encoding="utf-8")

    with pytest.raises(ValueError, match="clean Git tree"):
        release_build.create_source_archive(repo, "a" * 40, archive)

    assert archive.read_bytes() == b"owned"


def test_source_transfer_verifies_checksum_before_extracting(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"source")
    digest = digest_path(archive)
    provider = _ArchiveProvider(digest)
    request = object()

    release_build.stage_source_archive(
        provider,
        request,
        archive=archive,
        remote_archive="/srv/release/source.tar",
        remote_source_dir="/srv/release/source",
    )

    kinds = [action[0] for action in provider.actions]
    assert kinds == ["exec", "exec", "transfer", "exec", "exec"]
    assert provider.actions[-2][2] == ("sha256sum", "/srv/release/source.tar")
    extract = provider.actions[-1][2]
    assert isinstance(extract, tuple)
    assert isinstance(extract[2], str)
    assert extract[:2] == ("python3", "-c")
    assert 'filter="data"' in extract[2]
    assert extract[-2:] == ("/srv/release/source.tar", "/srv/release/source")


def test_source_transfer_rejects_checksum_mismatch_before_extracting(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"source")
    provider = _ArchiveProvider("sha256:" + "f" * 64)

    with pytest.raises(RuntimeError, match="source archive checksum mismatch"):
        release_build.stage_source_archive(
            provider,
            object(),
            archive=archive,
            remote_archive="/srv/release/source.tar",
            remote_source_dir="/srv/release/source",
        )

    assert not any(
        action[0] == "exec" and isinstance(action[2], tuple) and action[2][0] == "tar"
        for action in provider.actions
    )


def test_extract_commit_tree_ignores_worktree_only_paths(tmp_path: Path) -> None:
    """The extraction is the commit, so ignored and untracked junk cannot leak."""
    from nanolab.release.build import extract_commit_tree

    repo = tmp_path / "repo"
    (repo / "functions/python/solo").mkdir(parents=True)
    (repo / "functions/python/solo/function.yaml").write_text(
        "name: solo\n", encoding="utf-8"
    )
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        ("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(argv, cwd=repo, check=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Leftovers of the shape that broke the release: gitignored, tree stays clean.
    (repo / "functions/java/figlet/build").mkdir(parents=True)
    (repo / "functions/java/figlet/payloads").mkdir(parents=True)

    destination = extract_commit_tree(repo, commit, tmp_path / "tree")

    assert (destination / "functions/python/solo/function.yaml").is_file()
    assert not (destination / "functions/java/figlet").exists()


def test_extract_commit_tree_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    """A non-empty destination could merge leftovers into the extracted tree."""
    from nanolab.release.build import extract_commit_tree

    repo = tmp_path / "repo"
    (repo / "tracked.txt").parent.mkdir(parents=True, exist_ok=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        ("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(argv, cwd=repo, check=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    destination = tmp_path / "tree"
    destination.mkdir()
    (destination / "functions/java/figlet").mkdir(parents=True)

    with pytest.raises(ValueError, match="not empty"):
        extract_commit_tree(repo, commit, destination)

    # The pre-existing leftover must survive untouched, not get merged over.
    assert (destination / "functions/java/figlet").is_dir()


def test_extract_commit_tree_normalizes_git_failures_to_value_error(
    tmp_path: Path,
) -> None:
    from nanolab.release.build import extract_commit_tree

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)

    with pytest.raises(ValueError, match="could not extract release source"):
        extract_commit_tree(repo, "not-a-real-commit", tmp_path / "tree")


def test_source_python_tests_keep_generated_files_outside_archived_context():
    commands = release_build.source_test_commands(Path("/srv/release/source"))
    python = next(c for c in commands if c.task_id == "release.source.python-sdk")
    assert python.options.env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert (
        python.options.env["UV_PROJECT_ENVIRONMENT"]
        == "/srv/release/source-test-output/python-venv"
    )
    assert "cache_dir=/srv/release/source-test-output/pytest-cache" in python.argv[2]


def test_python_source_tests_isolate_editable_build_metadata(tmp_path: Path):
    source = tmp_path / "source"
    for name in (
        "sdks/python/tests",
        "sdks/runtime-contract",
        "functions/python/word-stats/tests",
        "functions/test-data",
    ):
        directory = source / name
        directory.mkdir(parents=True)
        (directory / "input.txt").write_text(name)
    (source / "pytest.ini").write_text("[pytest]\naddopts = --import-mode=importlib\n")
    sdk = source / "sdks/python/src"
    sdk.mkdir()
    (sdk / "sdk.py").write_text("committed source")
    before = {
        str(p.relative_to(source)): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    }
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    uv = binary_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "assert sys.argv[1:4] == ['run', '--project', 'sdks/python']\n"
        "assert Path('sdks/runtime-contract/input.txt').is_file()\n"
        "assert Path('functions/test-data/input.txt').is_file()\n"
        "assert '--import-mode=importlib' in Path('pytest.ini').read_text()\n"
        "metadata = Path('sdks/python/src/nanofaas_sdk.egg-info')\n"
        "metadata.mkdir()\n"
        "(metadata / 'PKG-INFO').write_text('editable build metadata')\n"
    )
    uv.chmod(0o755)
    command = next(
        c
        for c in release_build.source_test_commands(source)
        if c.task_id == "release.source.python-sdk"
    )
    env = {
        **os.environ,
        **command.options.env,
        "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
    }
    subprocess.run(command.argv, cwd=command.options.remote_dir, env=env, check=True)
    after = {
        str(p.relative_to(source)): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    }
    assert after == before, "editable install contaminated guarded recipe source"
    assert (
        source.parent
        / "source-test-output/python-source/sdks/python/src"
        / "nanofaas_sdk.egg-info/PKG-INFO"
    ).is_file()
