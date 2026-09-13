import io

import pytest


def test_bounded_reader_rejects_overflow():
    from nanolab.tasks.soak.collector import read_bounded

    assert read_bounded(io.BytesIO(b"abc"), 3) == b"abc"
    with pytest.raises(ValueError, match="collection body limit exceeded"):
        read_bounded(io.BytesIO(b"abcd"), 3)


def test_procfs_requires_container_ownership(tmp_path):
    from nanolab.tasks.soak.collector import collect_procfs

    proc = tmp_path / "42"
    proc.mkdir()
    (proc / "cgroup").write_text("0::/different-container\n")
    with pytest.raises(ValueError, match=r"cannot prove local procfs container"):
        collect_procfs(42, "a" * 64, tmp_path)


def test_procfs_handles_missing_pss_and_checks_identity(tmp_path):
    from nanolab.tasks.soak.collector import collect_procfs

    proc = tmp_path / "42"
    proc.mkdir()
    (proc / "cgroup").write_text("0::/docker/" + "a" * 64 + "\n")
    (proc / "stat").write_text("42 (java worker) S " + "0 " * 18 + "123 0\n")
    (proc / "status").write_text("VmRSS: 1 kB\n")
    result = collect_procfs(42, "a" * 64, tmp_path)
    assert result["status"] == "VmRSS: 1 kB\n"
    assert result["smaps_rollup"] is None
    assert result["start_ticks"] == "123"


def test_http_scheme_is_restricted():
    from nanolab.tasks.soak.collector import collect_http

    with pytest.raises(ValueError, match="metrics endpoint must use HTTP"):
        collect_http("file:///etc/passwd", 0.1)
