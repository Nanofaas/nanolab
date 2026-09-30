from __future__ import annotations

from nanolab.comparison.prepare import leftover_cleanup_operations


def test_the_cleanup_cannot_stop_a_run() -> None:
    """A fresh VM has no control plane and the functions are usually absent.

    Both are ordinary; neither may fail the matrix before it starts.
    """
    script = leftover_cleanup_operations(["word-stats-java"])[0].argv[2]

    assert script.endswith("|| true")
    assert " -m 5 " in script, "an unreachable control plane must not hang the run"


def test_a_cell_is_retried_once_and_not_more() -> None:
    """A dropped connection killed two k6 runs out of eight on Azure.

    Re-running a cell is safe: it has produced no summary yet, so the second
    attempt yields a whole valid cell rather than a mixture. But the SDK already
    keepalives the transport, so a connection that dies twice running is not a
    blip — the matrix should stop rather than grind through the rest producing
    nothing.
    """
    from nanolab.cli import comparison

    assert comparison.CELL_ATTEMPTS == 2


def test_multiple_profiles_share_one_captured_and_uploaded_source(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace
    from typing import cast

    from sonata_tasks.vm.ports import VmCommandProvider

    from nanolab.comparison.prepare import stage_comparison
    from nanolab.comparison.profiles import comparison_profiles
    from nanolab.tasks.vm.models import VmRequest

    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(("git", *args), cwd=source)

    git("init", "-q")
    git("config", "user.email", "tests@example.com")
    git("config", "user.name", "Tests")
    (source / "tracked").write_text("initial")
    git("add", ".")
    git("commit", "-qm", "initial")
    (source / "tracked").write_text("patch")

    class LocalVm:
        def __init__(self):
            self.commands = []
            self.uploads = []

        def exec_argv(self, request, argv, *, remote_dir=None, **kwargs):
            self.commands.append(argv)
            result = subprocess.run(
                argv, cwd=remote_dir, capture_output=True, text=True, check=False
            )
            return SimpleNamespace(
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        def transfer_to(self, request, *, source, destination):
            self.uploads.append(str(source))
            shutil.copyfile(source, destination)
            return SimpleNamespace(return_code=0)

    provider = LocalVm()
    request = VmRequest(
        lifecycle="multipass", name="test", home=str(tmp_path / "remote")
    )
    profiles = comparison_profiles(
        Path(__file__).resolve().parents[2], ("jvm-g1", "native-o3")
    )
    stage = stage_comparison(
        source=source,
        profiles=profiles,
        root=tmp_path / "run",
        tag="recipe-test",
        provider=cast(VmCommandProvider, provider),
        request=request,
    )
    assert list(stage.runs) == ["jvm", "jvm-g1", "native-o3"]
    assert len({run.source_dir for run in stage.runs.values()}) == 1
    assert len({run.output_dir for run in stage.runs.values()}) == 3
    assert (
        len([path for path in provider.uploads if path.endswith("source.tar.gz")]) == 1
    )
    assert len([argv for argv in provider.commands if argv[:2] == ("tar", "-xzf")]) == 1
    assert (
        (stage.remote_root / "profiles/jvm-g1.yaml")
        .as_posix()
        .endswith("profiles/jvm-g1.yaml")
    )
    assert (stage.runs["jvm"].source_dir / "tracked").read_text() == "patch"
    before = git("diff", "HEAD", "--binary")
    assert (
        subprocess.check_output(
            ("git", "diff", "HEAD", "--binary"), cwd=stage.remote_root / "source"
        )
        == before
    )
    stage.runs["jvm"].recipe.write_text("altered")
    import pytest

    with pytest.raises(ValueError, match="staged"):
        stage_comparison(
            source=source,
            profiles=profiles,
            root=tmp_path / "run",
            tag="recipe-test",
            provider=cast(VmCommandProvider, provider),
            request=request,
        )
