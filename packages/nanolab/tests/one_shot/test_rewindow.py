"""Preserve replay offsets while integrating a different decision grid."""

import copy
import unittest
from typing import cast

import pytest

from nanolab.tasks.one_shot.rewindow import rewindow_profile


def source_profile():
    return {
        "schemaVersion": 1,
        "periodSeconds": 360,
        "epochs": 1,
        "warmupSeconds": 720,
        "traceSha256": "a" * 64,
        "measurementCurves": {"edge/f": {"tag": "linear"}, "cloud/f": {"tag": "zero"}},
        "warmupCurves": {"edge/f": {"tag": "original-warmup"}},
        "cells": [
            {
                "phase": "campaign",
                "epoch": 0,
                "origin": "edge",
                "function": "f",
                "offsetsSeconds": [0, 59.9, 60, 359.9],
            },
            {
                "phase": "campaign",
                "epoch": 0,
                "origin": "cloud",
                "function": "f",
                "offsetsSeconds": [],
            },
            {
                "phase": "warmup",
                "epoch": -1,
                "origin": "edge",
                "function": "f",
                "offsetsSeconds": [-720, -0.1],
            },
            {
                "phase": "warmup",
                "epoch": -1,
                "origin": "cloud",
                "function": "f",
                "offsetsSeconds": [],
            },
        ],
    }


def integral(curve, start, end):
    # lambda(t) = t/360 requests/s: callback integrates in physical seconds.
    return (end**2 - start**2) / 720 if curve["tag"] == "linear" else 0


class RewindowTest(unittest.TestCase):
    def test_integrates_physical_minutes_and_keeps_every_offset(self):
        source = source_profile()
        saved = copy.deepcopy(source)
        profile, means = rewindow_profile(source, period_seconds=60, integral=integral)
        assert source == saved
        assert profile["epochs"] == 6
        assert profile["schemaVersion"] == 2
        assert profile["curveTimeScaleSeconds"] == 360
        assert [w["edge"]["f"] for w in means] == [(i + 0.5) / 6 for i in range(6)]
        assert sum(w["edge"]["f"] * 60 for w in means) == pytest.approx(180)
        cells = [
            c
            for c in profile["cells"]
            if c["phase"] == "campaign" and c["origin"] == "edge"
        ]
        assert [c["offsetsSeconds"] for c in cells] == [
            [0, 59.9],
            [60],
            [],
            [],
            [],
            [359.9],
        ]
        assert profile["measurementCurves"] == source["measurementCurves"]
        assert profile["warmupCurves"] == source["warmupCurves"]
        assert [c for c in profile["cells"] if c["phase"] == "warmup"] == source[
            "cells"
        ][2:]

    def test_constant_and_zero_rates_do_not_follow_integer_counts(self):
        def constant(curve, start, end):
            return (end - start) * 0.0123 if curve["tag"] == "linear" else 0

        profile, means = rewindow_profile(
            source_profile(), period_seconds=60, integral=constant
        )
        assert all(abs(w["edge"]["f"] - 0.0123) < 1e-15 for w in means)
        assert all(w["cloud"]["f"] == 0 for w in means)
        assert sum(len(c["offsetsSeconds"]) for c in profile["cells"]) == 6

    def test_rejects_invalid_period_and_outside_or_duplicate_offsets(self):
        for period in (True, 0, -60, 61, float("nan")):
            with (
                self.subTest(period=period),
                pytest.raises(
                    ValueError, match=r"period|calendar|scope|mean|quantum|offset"
                ),
            ):
                rewindow_profile(
                    source_profile(),
                    period_seconds=cast(int, period),
                    integral=integral,
                )
        for offsets in ([360], [-1], [0, 0], [1, 0], [float("nan")], [True]):
            source = source_profile()
            source["cells"][0]["offsetsSeconds"] = offsets
            with (
                self.subTest(offsets=offsets),
                pytest.raises(
                    ValueError, match=r"period|calendar|scope|mean|quantum|offset"
                ),
            ):
                rewindow_profile(source, period_seconds=60, integral=integral)

    def test_scope_duplicate_or_invalid_integral_does_not_drop_requests(self):
        for mutate in (
            lambda p: p["cells"].pop(),
            lambda p: p["cells"].append(p["cells"][0]),
            lambda p: p["measurementCurves"].pop("edge/f"),
        ):
            p = source_profile()
            mutate(p)
            with pytest.raises(
                ValueError, match=r"period|calendar|scope|mean|quantum|offset"
            ):
                rewindow_profile(p, period_seconds=60, integral=integral)
        for value in (-1, float("inf"), float("nan")):
            with pytest.raises(
                ValueError, match=r"period|calendar|scope|mean|quantum|offset"
            ):
                rewindow_profile(
                    source_profile(),
                    period_seconds=60,
                    integral=lambda *_, value=value: value,
                )


class QuantizedMeansTest(unittest.TestCase):
    def test_nearest_quantum_records_signed_error_without_minimum_load(self):
        from nanolab.tasks.one_shot.rewindow import quantize_means

        means = [{"edge": {"a": 0.0123, "b": 0.025, "c": 1.074, "d": 1.075, "e": 0}}]
        rates, audit = quantize_means(means, quantum=0.05, period_seconds=60)
        assert rates == [{"edge": {"a": 0, "b": 0.05, "c": 1.05, "d": 1.1, "e": 0}}]
        assert len(audit) == 5
        assert audit[0]["quantizationErrorRps"] == pytest.approx(-0.0123)
        assert audit[1]["meanRps"] == 0.025
        assert audit[1]["oracleRps"] == 0.05
        assert audit[1]["epoch"] == 0
        assert audit[1]["integratedRequests"] == 1.5

    def test_invalid_quantum_or_mean_is_not_silently_normalized(self):
        from nanolab.tasks.one_shot.rewindow import quantize_means

        for q in (0, -1, True, float("nan"), float("inf")):
            with (
                self.subTest(quantum=q),
                pytest.raises(
                    ValueError, match=r"period|calendar|scope|mean|quantum|offset"
                ),
            ):
                quantize_means([{"a": {"f": 1}}], quantum=q, period_seconds=60)
        for rate in (-1, float("nan"), float("inf"), True):
            with (
                self.subTest(rate=rate),
                pytest.raises(
                    ValueError, match=r"period|calendar|scope|mean|quantum|offset"
                ),
            ):
                quantize_means([{"a": {"f": rate}}], quantum=0.05, period_seconds=60)


def test_offline_cli_uses_real_splines_and_refuses_output_overwrite(tmp_path):
    import hashlib
    import json
    import os
    import subprocess
    from pathlib import Path

    import pytest

    python = os.environ.get("ONE_SHOT_CURVE_PYTHON")
    if not python:
        pytest.skip("set ONE_SHOT_CURVE_PYTHON to the existing SciPy environment")
    source = tmp_path / "parent"
    source.mkdir()
    profile = source_profile()
    profile["measurementCurves"] = {
        "edge/f": {
            "degree": 1,
            "knotsInIntervals": [0, 0, 1, 1],
            "coefficientsRps": [0, 1],
        },
        "cloud/f": {
            "degree": 1,
            "knotsInIntervals": [0, 0, 1, 1],
            "coefficientsRps": [0, 0],
        },
    }
    trace = {
        "schema_version": 1,
        "nodes": ["edge", "cloud"],
        "functions": ["f"],
        "period_seconds": 360,
        "flow_quantum": 0.05,
        "seed": 7,
        "payloads": {"f": {}},
        "windows": [{"edge": {"f": 0.5}, "cloud": {"f": 0}}],
    }

    def put(name, document):
        (source / name).write_text(json.dumps(document))
        return hashlib.sha256((source / name).read_bytes()).hexdigest()

    trace_hash = put("trace.json", trace)
    profile["traceSha256"] = trace_hash
    put("arrival-profile.json", profile)
    put("input-context.json", {"context": {"flowQuantum": 0.05}})
    hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()
    }
    put(
        "freeze.json",
        {"files": hashes, "arrivalProfile": {"sha256": hashes["arrival-profile.json"]}},
    )
    output = tmp_path / "derived with spaces"
    script = Path(__file__).parents[4] / "scripts/one-shot-rewindow.py"
    command = [python, str(script), "--freeze", str(source), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    means = json.loads((output / "means.json").read_text())
    assert means[0]["edge"]["f"] == pytest.approx(1 / 12)
    assert means[-1]["edge"]["f"] == pytest.approx(11 / 12)
    trace = json.loads((output / "trace.json").read_text())
    assert trace["period_seconds"] == 60
    assert trace["flow_quantum"] == 0.05
    assert trace["windows"][0]["edge"]["f"] == 0.1
    audit = json.loads((output / "verification.json").read_text())
    assert audit["measurementRequests"] == 4
    assert audit["warmupRequests"] == 2
    assert audit["calendarOffsetsIdentical"] is True
    assert audit["runtimeReady"] is False
    before = (output / "means.json").read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (output / "means.json").read_bytes() == before
    # Hash checks run before output creation, including unchanged source trace identity.
    (source / "trace.json").write_text("{}")
    command[-1] = str(tmp_path / "invalid-source")
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert not (tmp_path / "invalid-source").exists()


def test_records_actual_reference_interpreter_and_packages():
    import os
    import runpy
    from pathlib import Path

    python = os.environ.get("ONE_SHOT_CURVE_PYTHON")
    if not python:
        pytest.skip("set ONE_SHOT_CURVE_PYTHON to the existing SciPy environment")
    script = Path(__file__).parents[4] / "scripts/one-shot-rewindow.py"
    probe = runpy.run_path(str(script))["reference_environment"]
    environment = probe(Path(python), ["numpy", "scipy"])
    assert environment["executable"] == python
    assert environment["packages"] == {"numpy": "2.2.6", "scipy": "1.15.3"}
    assert environment["python"].startswith("3.12.")
