import copy
from datetime import UTC, datetime

import pytest

from nanolab.tasks.one_shot.arrivals import retime_schedule, validate_arrival_profile
from nanolab.tasks.one_shot.trace import TraceArtifact, request_schedule


def arrival_case_data():
    trace = TraceArtifact(
        nodes=["a", "cloud"],
        functions=["f", "g"],
        period_seconds=360,
        flow_quantum=1 / 360,
        seed=1,
        payloads={"f": {}, "g": {}},
        windows=[{"a": {"f": 12 / 360, "g": 0}, "cloud": {"f": 1 / 360, "g": 0}}] * 9,
    )
    cells = []
    for epoch, window in enumerate(trace.windows):
        for node, rates in window.items():
            for function, rate in rates.items():
                count = round(rate * 360)
                cells.append(
                    {
                        "phase": "campaign",
                        "epoch": epoch,
                        "origin": node,
                        "function": function,
                        "offsetsSeconds": [
                            epoch * 360 + ((i + 0.5) / count) ** 2 * 360
                            for i in range(count)
                        ],
                    }
                )
    for node, rates in trace.windows[0].items():
        for function, rate in rates.items():
            count = round(rate * 720)
            cells.append(
                {
                    "phase": "warmup",
                    "epoch": -1,
                    "origin": node,
                    "function": function,
                    "offsetsSeconds": [
                        -720 + (i + 0.5) / count * 720 for i in range(count)
                    ],
                }
            )
    profile = {
        "schemaVersion": 1,
        "periodSeconds": 360,
        "epochs": 9,
        "warmupSeconds": 720,
        "traceSha256": "a" * 64,
        "cells": cells,
    }
    return trace, profile


@pytest.fixture
def arrival_case():
    return arrival_case_data()


def test_retime_preserves_fields_input_and_numeric_request_order(arrival_case):
    trace, profile = arrival_case
    validate_arrival_profile(profile, trace, trace_sha256="a" * 64, warmup_seconds=720)
    anchor = datetime(2026, 10, 13, tzinfo=UTC)
    original = request_schedule(
        trace,
        anchor=anchor,
        endpoints={"a": "http://a", "cloud": "http://cloud"},
        run_id="run",
    )
    saved = copy.deepcopy(original)
    # The adapter can consume a campaign-only subset for isolated tests.
    campaign = dict(
        profile, cells=[x for x in profile["cells"] if x["phase"] == "campaign"]
    )
    result = retime_schedule(original, campaign, anchor_timestamp=anchor.timestamp())
    assert original == saved
    assert len(result) == len(original)
    before = {x["originalId"]: x for x in original}
    for row in result:
        old = before[row["originalId"]]
        assert {k: v for k, v in row.items() if k != "scheduledAt"} == {
            k: v for k, v in old.items() if k != "scheduledAt"
        }
    a = [x for x in result if x["origin"] == "a" and x["epoch"] == 0]
    assert [x["originalId"].split(":")[-1] for x in a] == [str(i) for i in range(12)]
    assert a[0]["scheduledAt"] != before[a[0]["originalId"]]["scheduledAt"]


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "missing",
        "duplicate",
        "extra",
        "count",
        "order",
        "nan",
        "inf",
        "bool",
        "boundary",
        "warmup",
        "epoch-bool",
    ],
)
def test_invalid_profile_is_rejected(arrival_case, mutation):
    trace, profile = arrival_case
    p = copy.deepcopy(profile)
    offsets = p["cells"][0]["offsetsSeconds"]
    if mutation == "hash":
        p["traceSha256"] = "b" * 64
    if mutation == "missing":
        p["cells"].pop()
    if mutation == "duplicate":
        p["cells"].append(copy.deepcopy(p["cells"][0]))
    if mutation == "extra":
        p["cells"][0]["origin"] = "unknown"
    if mutation == "count":
        offsets.pop()
    if mutation == "order":
        offsets[1] = offsets[0]
    if mutation == "nan":
        offsets[0] = float("nan")
    if mutation == "inf":
        offsets[0] = float("inf")
    if mutation == "bool":
        offsets[0] = True
    if mutation == "boundary":
        offsets[-1] = 360
    if mutation == "warmup":
        p["warmupSeconds"] = 721
    if mutation == "epoch-bool":
        p["cells"][0]["epoch"] = False
    with pytest.raises(
        ValueError, match=r"arrival|profile|offset|cell|scope|warmup|trace"
    ):
        validate_arrival_profile(p, trace, trace_sha256="a" * 64, warmup_seconds=720)


@pytest.mark.parametrize("document", [None, [], 1, True])
def test_non_object_profile_is_rejected(arrival_case, document):
    trace, _ = arrival_case
    with pytest.raises(ValueError, match="arrival"):
        validate_arrival_profile(
            document, trace, trace_sha256="a" * 64, warmup_seconds=720
        )


def test_frozen_profile_is_portable_and_requires_its_identity(arrival_case, tmp_path):
    import hashlib
    import json
    import shutil

    from nanolab.tasks.one_shot.arrivals import read_frozen_arrival_profile

    trace, profile = arrival_case
    original = tmp_path / "original"
    original.mkdir()
    trace_bytes = trace.model_dump_json().encode()
    (original / "trace.json").write_bytes(trace_bytes)
    profile["traceSha256"] = hashlib.sha256(trace_bytes).hexdigest()
    profile_bytes = json.dumps(profile).encode()
    (original / "arrival-profile.json").write_bytes(profile_bytes)
    digest = hashlib.sha256(profile_bytes).hexdigest()
    frozen = {
        "files": {"arrival-profile.json": digest},
        "arrivalProfile": {"sha256": digest},
    }
    relocated = tmp_path / "relocated with spaces"
    shutil.move(original, relocated)
    assert read_frozen_arrival_profile(relocated, frozen) == profile
    del frozen["arrivalProfile"]
    with pytest.raises(ValueError, match="arrival"):
        read_frozen_arrival_profile(relocated, frozen)
