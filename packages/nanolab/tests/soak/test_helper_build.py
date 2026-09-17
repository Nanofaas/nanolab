"""The helper image is built per run and its digest frozen from that build."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from nanolab.tasks.soak.helper_build import (
    BASES_LOCK,
    BUILD_CONTEXT,
    DOCKERFILE,
    MAT_LOCK,
    HelperImageError,
    HelperImageRequest,
    build_arguments,
    build_helper_image,
)

DIGEST = "sha256:" + "c" * 64


def _request(tmp_path: Path, **overrides: object) -> HelperImageRequest:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "run_dir": run_dir,
        "run_id": "heap-analysis-abc123",
        "registry": "localhost:5000/nanolab",
        "builder": "nanolab-heap-analysis",
    }
    fields.update(overrides)
    return HelperImageRequest(**fields)  # type: ignore[arg-type]


def _fake_docker(tmp_path: Path, *, digest: str | None = DIGEST, code: int = 0) -> Path:
    """Stand in for docker: record argv and write a build's metadata file."""
    script = tmp_path / "docker"
    payload = json.dumps({"containerimage.digest": digest} if digest else {})
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text("
        "json.dumps(sys.argv[1:]))\n"
        "argv = sys.argv[1:]\n"
        "meta = argv[argv.index('--metadata-file') + 1]\n"
        f"pathlib.Path(meta).write_text({payload!r})\n"
        f"sys.exit({code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _argv(tmp_path: Path) -> list[str]:
    return json.loads((tmp_path / "argv.json").read_text())


def test_the_checked_in_locks_supply_every_build_argument() -> None:
    """The real lock files must actually drive the build, not just exist."""
    arguments = build_arguments()
    assert set(arguments) == {"MAT_URL", "MAT_SHA256", "JDK_BASE", "PYTHON_BASE"}
    mat = json.loads(MAT_LOCK.read_text())
    assert arguments["MAT_URL"] == mat["url"]
    assert arguments["MAT_SHA256"] == mat["sha256"]
    bases = json.loads(BASES_LOCK.read_text())
    assert arguments["JDK_BASE"] == bases["jdk_base"]["reference"]
    assert arguments["PYTHON_BASE"] == bases["python_base"]["reference"]
    for name in ("JDK_BASE", "PYTHON_BASE"):
        assert "@sha256:" in arguments[name]


def test_build_publishes_and_returns_the_digest_it_just_pushed(tmp_path) -> None:
    docker = _fake_docker(tmp_path)
    request = _request(tmp_path, docker=str(docker))

    resolved = build_helper_image(request)

    assert resolved == f"localhost:5000/nanolab/diagnostic-helper@{DIGEST}"
    argv = _argv(tmp_path)
    assert argv[:3] == ["buildx", "build", "--builder"]
    assert "--push" in argv
    assert "--provenance=mode=max" in argv
    assert "--build-arg" in argv
    joined = " ".join(argv)
    for name in ("MAT_URL", "MAT_SHA256", "JDK_BASE", "PYTHON_BASE"):
        assert f"{name}=" in joined


def test_the_digest_comes_from_this_build_not_a_registry_lookup(tmp_path) -> None:
    """A later registry query could resolve a tag someone else just moved."""
    docker = _fake_docker(tmp_path)
    request = _request(tmp_path, docker=str(docker))

    build_helper_image(request)

    argv = _argv(tmp_path)
    assert "--metadata-file" in argv
    metadata = Path(argv[argv.index("--metadata-file") + 1])
    assert json.loads(metadata.read_text())["containerimage.digest"] == DIGEST


def test_a_build_that_publishes_no_digest_is_an_error(tmp_path) -> None:
    docker = _fake_docker(tmp_path, digest=None)
    with pytest.raises(HelperImageError, match="no image digest"):
        build_helper_image(_request(tmp_path, docker=str(docker)))


def test_a_failing_build_never_returns_a_reference(tmp_path) -> None:
    docker = _fake_docker(tmp_path, code=1)
    with pytest.raises(HelperImageError, match="did not complete cleanly"):
        build_helper_image(_request(tmp_path, docker=str(docker)))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("registry", "https://localhost:5000/nanolab", "not a URL"),
        ("run_id", "bad id", "tag-safe"),
    ],
)
def test_request_rejects_unusable_inputs(tmp_path, field, value, message) -> None:
    with pytest.raises(ValueError, match=message):
        _request(tmp_path, **{field: value})


def test_the_build_context_resolves_from_this_package(tmp_path) -> None:
    """The context must be nanolab's own package, wherever it is checked out.

    nanolab was once a subdirectory of the nanoFaaS checkout, so a
    caller-supplied repo_root was correct then. Extraction to a standalone
    workspace left that root naming a path with no `packages/nanolab` in it,
    and every test in this file still passed because each one built its own
    fake context -- so the production path was never the path under test. A
    real `run` is what found it. This pins the real, derived context instead.
    """
    _request(tmp_path)  # raises if the derived context has no Dockerfile

    assert (BUILD_CONTEXT / DOCKERFILE).is_file()
    assert BUILD_CONTEXT.parts[-2:] == ("packages", "nanolab")


def _soak_config(**diagnostics: object):
    """Load the checked-in candidate protocol, tweaking its diagnostics block."""
    import yaml

    from nanolab.config.soak import SoakConfig

    path = (
        Path(__file__).resolve().parents[2]
        / "scenarios-v2"
        / "memory-soak-sync-candidate-diagnostic-container.yaml"
    )
    raw = yaml.safe_load(path.read_text())["soak"]
    if not raw["criteria"]:
        # The candidate takes its criteria from a policy file, as the existing
        # soak fixtures do; backfill them so the protocol validates standalone.
        smoke = path.with_name("memory-soak-smoke-container.yaml")
        raw["criteria"] = yaml.safe_load(smoke.read_text())["soak"]["criteria"]
    raw["diagnostics"].update(diagnostics)
    return SoakConfig.model_validate(raw)


def _stamp(tmp_path: Path, config, **options: object):
    """Run the soak helper stamp with a fake builder, returning the new config."""
    from nanolab.tasks.soak.runtime import RuntimeOptions, _with_built_helper

    # Deliberately not created: `_with_built_helper` must create the run root
    # itself, because the default diagnostic protocols carry no policy file and
    # so nothing else does. Pre-creating it here would hide that.
    run_dir = tmp_path / "run"
    return _with_built_helper(
        config,
        run_dir=run_dir,
        options=RuntimeOptions(**options),  # type: ignore[arg-type]
    )


def test_soak_stamps_the_run_built_digest_into_every_diagnosed_role(
    tmp_path, monkeypatch
) -> None:
    """The checked-in protocol carries no helper, so the run must supply one."""
    built = "localhost:5000/nanolab/diagnostic-helper@" + DIGEST
    monkeypatch.setattr(
        "nanolab.tasks.soak.helper_build.build_helper_image", lambda request: built
    )
    config = _soak_config()
    assert config.diagnostics.helper_images == {}

    stamped = _stamp(tmp_path, config)

    diagnosed = {
        role for role, operations in config.diagnostics.operations.items() if operations
    }
    assert diagnosed
    assert stamped.diagnostics.helper_images == dict.fromkeys(diagnosed, built)


def test_soak_leaves_a_pinned_helper_alone(tmp_path, monkeypatch) -> None:
    """An operator who pinned one deliberately still gets exactly that one."""
    monkeypatch.setattr(
        "nanolab.tasks.soak.helper_build.build_helper_image",
        lambda request: pytest.fail("a pinned helper must not trigger a build"),
    )
    pinned = "localhost:5000/nanolab/p24-diagnostic-helper@" + DIGEST
    config = _soak_config(helper_images={"control-plane": pinned})

    stamped = _stamp(tmp_path, config)

    assert stamped.diagnostics.helper_images == {"control-plane": pinned}


def test_soak_leaves_an_injected_memory_helper_alone(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "nanolab.tasks.soak.helper_build.build_helper_image",
        lambda request: pytest.fail("an injected helper must not trigger a build"),
    )
    config = _soak_config()

    stamped = _stamp(
        tmp_path,
        config,
        memory_helper_image="localhost:5000/nanolab/x@" + DIGEST,
    )

    assert stamped.diagnostics.helper_images == {}


def test_a_protocol_that_diagnoses_nothing_builds_no_helper(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "nanolab.tasks.soak.helper_build.build_helper_image",
        lambda request: pytest.fail("no diagnostics means no helper to build"),
    )
    config = _soak_config(operations={})

    assert _stamp(tmp_path, config).diagnostics.helper_images == {}


def test_request_rejects_a_context_without_the_dockerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The missing-Dockerfile guard must still be reachable and still refuse.

    The context is derived from this file now, so no real checkout can miss the
    Dockerfile and the guard would otherwise stop being exercised at all. Point
    the derived context at a bare directory to reach it.
    """
    from nanolab.tasks.soak import helper_build

    monkeypatch.setattr(helper_build, "BUILD_CONTEXT", tmp_path)
    with pytest.raises(ValueError, match="Dockerfile is missing"):
        _request(tmp_path)
