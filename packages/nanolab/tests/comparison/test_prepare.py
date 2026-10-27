from __future__ import annotations

from nanolab.comparison.prepare import leftover_cleanup_operations
from nanolab.workspace.paths import discover_tool_root


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
    profiles = comparison_profiles(discover_tool_root(), ("jvm-g1", "native-o3"))
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


class _Remote:
    """A VM whose uploads may fail or arrive altered, answering `sha256sum`."""

    def __init__(self, *, upload_code: int = 0, arrives: bytes | None = None):
        self.files: dict[str, bytes] = {}
        self.upload_code = upload_code
        self.arrives = arrives

    def transfer_to(self, request, *, source, destination):
        from types import SimpleNamespace

        self.files[destination] = (
            self.arrives if self.arrives is not None else source.read_bytes()
        )
        return SimpleNamespace(return_code=self.upload_code)

    def transfer_from(self, request, *, source, destination):
        from types import SimpleNamespace

        if source not in self.files:
            return SimpleNamespace(return_code=1)
        destination.write_bytes(self.files[source])
        return SimpleNamespace(return_code=0)

    def exec_argv(self, request, argv, **kwargs):
        import hashlib
        from types import SimpleNamespace

        data = self.files.get(argv[1])
        if argv[0] != "sha256sum" or data is None:
            return SimpleNamespace(return_code=1, stdout="", stderr="missing")
        digest = hashlib.sha256(data).hexdigest()
        return SimpleNamespace(
            return_code=0, stdout=f"{digest}  {argv[1]}\n", stderr=""
        )


def test_verified_transfer_rejects_upload_failure_or_hash_mismatch(tmp_path):
    """Preparation and the experiment-02 generator upload through one function."""
    from pathlib import PurePosixPath
    from typing import cast

    import pytest
    from sonata_tasks.vm.ports import VmCommandProvider

    from nanolab.comparison.prepare import comparison_transfer_to
    from nanolab.tasks.vm.models import VmRequest

    local = tmp_path / "experiment-01.js"
    local.write_text("export default function () {}\n")
    remote = PurePosixPath("/home/azureuser/e02/experiment-01.js")
    request = VmRequest(lifecycle="multipass", name="loadgen")

    def upload(vm: _Remote) -> None:
        comparison_transfer_to(cast(VmCommandProvider, vm), request, local, remote)

    upload(_Remote())
    with pytest.raises(RuntimeError, match="transfer failed"):
        upload(_Remote(upload_code=1))
    with pytest.raises(ValueError, match="differs"):
        upload(_Remote(arrives=b"changed"))


def test_download_fails_closed_and_creates_the_directory(tmp_path):
    from pathlib import PurePosixPath
    from typing import cast

    import pytest
    from sonata_tasks.vm.ports import VmCommandProvider

    from nanolab.comparison.prepare import comparison_transfer_from
    from nanolab.tasks.vm.models import VmRequest

    vm = _Remote()
    vm.files["/remote/samples.jsonl"] = b"{}\n"
    request = VmRequest(lifecycle="multipass", name="stack")
    provider = cast(VmCommandProvider, vm)
    local = tmp_path / "metrics" / "samples.jsonl"
    comparison_transfer_from(
        provider, request, PurePosixPath("/remote/samples.jsonl"), local
    )
    assert local.read_bytes() == b"{}\n"
    with pytest.raises(RuntimeError, match="transfer failed"):
        comparison_transfer_from(
            provider, request, PurePosixPath("/remote/missing"), tmp_path / "x"
        )
