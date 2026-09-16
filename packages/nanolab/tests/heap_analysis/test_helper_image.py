"""The helper image is built per run and its digest frozen from that build."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from nanolab.tasks.heap_analysis.helper_image import (
    BASES_LOCK,
    MAT_LOCK,
    HelperImageError,
    HelperImageRequest,
    build_arguments,
    build_helper_image,
)

DIGEST = "sha256:" + "c" * 64


def _repo(tmp_path: Path) -> Path:
    context = tmp_path / "packages" / "nanolab" / "assets" / "soak"
    context.mkdir(parents=True)
    (context / "diagnostic-helper.Dockerfile").write_text("FROM scratch\n")
    return tmp_path


def _request(tmp_path: Path, **overrides: object) -> HelperImageRequest:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "repo_root": _repo(tmp_path),
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

    assert resolved == f"localhost:5000/nanolab/heap-analysis-helper@{DIGEST}"
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


def test_request_rejects_a_context_without_the_dockerfile(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="Dockerfile is missing"):
        HelperImageRequest(
            repo_root=tmp_path,
            run_dir=run_dir,
            run_id="heap-analysis-abc123",
            registry="localhost:5000/nanolab",
            builder="nanolab-heap-analysis",
        )
