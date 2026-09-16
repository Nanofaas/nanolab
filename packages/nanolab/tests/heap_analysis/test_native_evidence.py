import json

import pytest

from nanolab.tasks.heap_analysis.evidence import native_comparison, persist_native
from nanolab.tasks.soak.artifacts import ArtifactLimitExceededError


def reading():
    return {
        "status": "RssAnon: 4 kB\n",
        "smaps_rollup": "Pss_Anon: 4 kB\n",
        "smaps": "1000-2000 rw-p 0 00:00 0\nSize: 4 kB\nRss: 4 kB\nPss: 4 kB\n",
        "heap_info": "garbage-first heap total 1024K, used 512K\n",
        "intervals": {"status": {"started_s": 1.0, "ended_s": 2.0}},
        "completion": {"heap_info": "completed"},
        "errors": {},
        "started_s": 1.0,
        "ended_s": 4.0,
    }


def test_persist_native_keeps_all_sources_and_intervals(tmp_path):
    root = tmp_path / "evidence"
    raw = reading()
    block = persist_native(root, "natural-drain", raw, 1048576)
    for key in ("status", "smaps_rollup", "smaps", "heap_info"):
        source = block["sources"][key]
        assert (root / source["artifact"]["path"]).read_text() == raw[key]
    assert block["sources"]["status"]["interval"] == raw["intervals"]["status"]
    assert block["sources"]["heap_info"]["completion"] == "completed"
    assert block["collection"]["ended_s"] == 4.0


def test_raw_writes_respect_cumulative_budget_before_writing(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    (tmp_path / "other-artifact").write_bytes(b"x" * 1024)
    with pytest.raises(ArtifactLimitExceededError):
        persist_native(root, "natural-drain", reading(), 1024 + 4096)
    assert not list(root.rglob("*.txt"))


def test_missing_source_is_visible_and_not_written(tmp_path):
    raw = reading()
    raw["smaps"] = None
    raw["errors"]["smaps"] = "read bound exceeded"
    block = persist_native(tmp_path / "evidence", "natural-drain", raw, 1048576)
    assert block["smaps"]["available"] is False
    assert "artifact" not in block["sources"]["smaps"]
    assert block["sources"]["smaps"]["error"] == "read bound exceeded"
    assert block["residency"]["RssAnon"] == 4096


def test_comparison_retains_missing_and_malformed_checkpoints(tmp_path):
    (tmp_path / "runtime-natural-drain.json").write_text("not JSON")
    (tmp_path / "runtime-after-final-gc.json").write_text(
        json.dumps(
            {
                "native": {"residency": {"RssAnon": 4096}},
            }
        )
    )
    comparison = native_comparison(tmp_path)
    assert set(comparison) == {"before-baseline", "natural-drain", "after-final-gc"}
    assert comparison["before-baseline"]["available"] is False
    assert comparison["natural-drain"]["available"] is False
    assert comparison["after-final-gc"]["native"]["residency"]["RssAnon"] == 4096
    assert "before final dump" in comparison["after-final-gc"]["phase"]
