"""Synthetic producer-to-evaluator integration; no Docker, builds or network."""

import asyncio
import dataclasses
import json
import shutil
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast

import pytest

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.artifacts import ArtifactWriter, describe_artifact, fingerprint
from nanolab.tasks.soak.evaluate import combine_results, evaluate_run
from nanolab.tasks.soak.images import BuildRecipe, freeze_build_receipt
from nanolab.tasks.soak.models import Sample, Target
from nanolab.tasks.soak.preflight import preflight
from nanolab.tasks.soak.report import write_report
from nanolab.tasks.soak.sources import SourceEntry

SCHEMA = "nanolab-soak-v1"
HELPER = "jdk@sha256:" + "b" * 64


def save(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return reference(root, name)


def reference(root, name):
    value = describe_artifact(root / name)
    value["path"] = name
    return value


def protocol():
    roles = ("control-plane", "fn")
    return {
        "purpose": "p24",
        "phases": {
            "warmup_s": 1,
            "baseline_drain_s": 3,
            "baseline_window_s": 2,
            "steady_s": 6,
            "drain_s": 3,
            "cleanup_margin_s": 1,
        },
        "retention_s": {"payloads": 2},
        "roles": {
            r: {
                "runtime": "jvm",
                "expected_cpu": 1.0,
                "memory_limit_bytes": 1000,
                "required_metrics": [
                    "process_rss_bytes",
                    "cgroup_memory_usage_bytes",
                    "pending",
                ],
                "required_capabilities": ["rss", "cgroup"],
                "runtime_options": [],
                "collection_sources": ["procfs", "cgroup", "prometheus"],
            }
            for r in roles
        },
        "images": {r: {"variant": "jvm", "platform": "linux/amd64"} for r in roles},
        "criteria": [
            c
            for r in roles
            for c in (
                {
                    "id": r + ".budget",
                    "role": r,
                    "metric": "cgroup_memory_usage_bytes",
                    "unit": "bytes",
                    "operation": "maximum",
                    "phase": "steady",
                    "window_s": 6,
                    "threshold": 1000,
                    "rationale": "frozen memory budget",
                },
                {
                    "id": r + ".rss",
                    "role": r,
                    "metric": "process_rss_bytes",
                    "unit": "bytes",
                    "operation": "return_to_reference",
                    "phase": "drain",
                    "window_s": 3,
                    "deadline_s": 3,
                    "absolute_tolerance": 10,
                    "relative_tolerance": 0.1,
                    "rationale": "natural recovery",
                },
            )
        ],
        "diagnostics": {
            "operations": {},
            "timeout_s": 2,
            "max_dumps": 0,
            "max_dump_bytes": 0,
        },
        "prerequisites": {
            "required_coverage": ["sync"],
            "relevant_config_keys": {"sync": ["roles", "workload"]},
        },
        "sample_interval_s": 1,
        "scrape_timeout_s": 1,
        "max_observation_gap_s": 1.1,
        "artifact_limit_bytes": 1000000,
        "cancellation_timeout_s": 2,
        "workload": {
            "rates": {"fn": 2},
            "preallocated_vus": 1,
            "max_vus": 1,
            "max_error_ratio": 0,
            "max_dropped_iterations": 0,
        },
    }


def persist(run):
    root, manifest = run["root"], run["manifest"]
    for key in ("config", "preflight", "workload", "prerequisites"):
        name = manifest[key]["path"]
        manifest[key] = save(root, name, run[key])
    save(root, "evaluation-input.json", run["projection"])
    (root / "samples.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in run["samples"])
    )
    manifest["diagnostics"] = save(root, "diagnostics.json", run["diagnostics"])
    entries = []
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if (
            path.is_file()
            and name not in {"acceptance-manifest.json", "artifacts.json"}
            and not name.startswith("evaluations/")
        ):
            entries.append(reference(root, name))
    inventory = {
        "schema": SCHEMA,
        "run_id": manifest["run_id"],
        "policy_sha256": manifest["policy_sha256"],
        "complete": True,
        "budget_exhausted": False,
        "entries": entries,
    }
    manifest["artifacts"] = save(root, "artifacts.json", inventory)
    save(root, "acceptance-manifest.json", manifest)


def make_run(
    tmp_path,
    monkeypatch,
    *,
    smoke=False,
    diagnostic=False,
    growth=False,
    bad_profile=False,
):
    import nanolab.tasks.soak.prerequisites as prerequisite_module
    import nanolab.tasks.soak.workload as workload_module

    raw = protocol()
    if smoke:
        raw["purpose"] = "smoke"
    if diagnostic or growth:
        raw["diagnostics"].update(
            operations={"control-plane": ["gc"]},
            helper_images={"control-plane": HELPER},
            gc_completion_evidence={"control-plane": "full-cycle"},
        )
    if growth:
        rss = raw["criteria"][1]
        rss.update(operation="growth_review", threshold=5)
        del rss["absolute_tolerance"], rss["relative_tolerance"]
        raw["criteria"].append(
            {
                "id": "rss.maximum",
                "role": "control-plane",
                "metric": "process_rss_bytes",
                "unit": "bytes",
                "operation": "maximum",
                "phase": "drain",
                "window_s": 3,
                "threshold": 200,
                "rationale": "frozen retained population ceiling",
            }
        )
    model = SoakConfig.model_validate(raw)
    config = model.model_dump(mode="json")
    targets = [
        asdict(Target(r, r, i + 1, "started", r + "@sha256:" + "a" * 64, "jvm"))
        for i, r in enumerate(config["roles"])
    ]
    binding = {
        "schema": SCHEMA,
        "run_id": "synthetic-run",
        "policy_sha256": fingerprint(config),
    }
    phases = {
        "warmup": {"start_s": 2, "end_s": 3},
        "baseline_drain": {"start_s": 3, "end_s": 6},
        "baseline": {"start_s": 6, "end_s": 8},
        "steady": {"start_s": 8, "end_s": 14},
        "drain": {"start_s": 14, "end_s": 17},
    }
    manifest = {
        **binding,
        "completed": True,
        "aborted": False,
        "phases": phases,
        "frozen_at_s": 0,
        "builds_finished_s": 1,
        "preflight_started_s": 0,
        "preflight_ended_s": 1,
        "observation_started_s": 2,
        "observation_ended_s": 17,
        "traffic_stopped_s": 14,
        "natural_drain": True,
        "restarts": [],
        "final_targets": targets,
        "builds": {},
        "recipes": {},
    }
    manifest["config"] = save(tmp_path, "config.json", config)
    tree = tmp_path / "source/tree"
    tree.mkdir(parents=True)
    (tree / "input.txt").write_text("synthetic source input")
    stat = describe_artifact(tree / "input.txt")
    entry = SourceEntry(
        "input.txt",
        "file",
        (tree / "input.txt").stat().st_mode & 0o777,
        stat["size_bytes"],
        stat["sha256"],
    )
    entries = [asdict(entry)]
    source_hash = fingerprint({"entries": entries})
    (tree.parent / "source-manifest.jsonl").write_text(json.dumps(entries[0]) + "\n")
    manifest["source"] = save(
        tmp_path,
        "source/snapshot.json",
        {
            "schema": SCHEMA,
            "status": "captured",
            "root": str(tree),
            "revision": None,
            "dirty": True,
            "fingerprint": source_hash,
            "manifest_sha256": describe_artifact(tree.parent / "source-manifest.jsonl")[
                "sha256"
            ],
            "entry_count": 1,
        },
    )
    build_receipts = {}
    for target in targets:
        role = target["role"]
        argv = (
            ("./gradlew", "-PcontrolPlaneModules=none")
            if role == "control-plane"
            else None
        )
        definition = {"context": str(tree), "platforms": ["linux/amd64"], "args": {}}
        identity = fingerprint(
            {
                "role": role,
                "variant": "jvm",
                "platform": "linux/amd64",
                "prerequisite": list(argv) if argv else None,
                "build": definition,
            }
        )
        recipe = BuildRecipe(
            role,
            "build",
            "jvm",
            "linux/amd64",
            role + ":synthetic",
            argv,
            {"target": {role: definition}},
            identity,
            None,
        )
        manifest["recipes"][role] = save(
            tmp_path, "build/recipe-" + role + ".json", asdict(recipe)
        )
        log = tmp_path / ("build/observed-" + role + ".json")
        log.write_text(
            json.dumps(
                {
                    "source": source_hash,
                    "output": target["image_digest"],
                    "fixture": "synthetic build observer",
                }
            )
        )
        built = freeze_build_receipt(
            recipe,
            source_hash,
            "sha256:" + "a" * 64,
            toolchains={"compiler": "synthetic-compiler-1"},
            base_images={"runtime": HELPER},
            logs=(log,),
        )
        data = json.loads(json.dumps(asdict(built)))
        build_receipts[role] = data
        manifest["builds"][role] = save(
            tmp_path, "build/receipt-" + role + ".json", {"schema": SCHEMA, **data}
        )
    observations = {
        "snapshot_fingerprint": source_hash,
        "retention_s": config["retention_s"],
        "free_bytes": 2000000,
        "generator": {"available": True, "max_vus": 1},
        "build_receipts": build_receipts,
        "roles": {
            t["role"]: {
                "cpu": 1.0,
                "memory_bytes": 1000,
                "limit_sources": {"cpu": "cpu.max", "memory_bytes": "memory.max"},
                "image_digest": t["image_digest"],
                "runtime": "jvm",
                "runtime_options": [],
                "metrics": config["roles"][t["role"]]["required_metrics"],
                "capabilities": ["rss", "cgroup"],
                "collection_sources": ["procfs", "cgroup", "prometheus"],
                "diagnostics": config["diagnostics"]["operations"].get(t["role"], []),
                "gc_completion_evidence": "full-cycle",
                "modules": [],
            }
            for t in targets
        },
    }
    writer = ArtifactWriter(tmp_path / "preflight", 100000)
    preflight(model, tuple(Target(**t) for t in targets), observations, writer)
    writer.close()
    manifest["preflight"] = reference(tmp_path, "preflight/preflight.json")
    generator = tmp_path / "fake-k6.py"
    generator.write_text("""import json, os
from pathlib import Path
cfg = json.loads(Path(os.environ["NANOLAB_SOAK_CONFIG"]).read_text())
metrics = {}
counts = (("offered",12),("success",12),("error",0),
          ("retry",0),("replay",0),("dropped",0))
for key, count in counts:
    name = "dropped_iterations" if key == "dropped" else "soak_" + key
    metrics[name] = {"values": {"count": count * len(cfg["functions"])}}
    for function in cfg["functions"]:
        suffix = "{scenario:" + function["scenario"] + "}"
        metrics[name + suffix] = {"values": {"count": count}}
passed = 12 * len(cfg["functions"])
summary = {"metrics": metrics, "root_group": {"checks": [
    {"name":"status is 200", "passes":passed, "fails":0},
    {"name":"has expected success response", "passes":passed, "fails":0}]}}
marker = os.environ["NANOLAB_SOAK_SUMMARY_MARKER"]
print("\\n" + marker + ":START\\n" + json.dumps(summary)
      + "\\n" + marker + ":END\\n", flush=True)
""")
    workload_root = tmp_path / "load"
    clock = SimpleNamespace(monotonic=lambda: 8.0)

    class VirtualClockRunner(workload_module.OwnedCommandRunner):
        def run(self):
            # Run the actual descendant owner and retain every release/quota
            # observation. Map only its timestamp into the synthetic phase clock.
            return replace(super().run(), ended_s=14.0)

    with monkeypatch.context() as patch:
        patch.setattr(workload_module, "time", clock)
        patch.setattr(workload_module, "OwnedCommandRunner", VirtualClockRunner)
        driver = workload_module.K6WorkloadDriver(
            base_url="http://127.0.0.1:1",
            function_rates={"fn": 2},
            payloads={"fn": [{"input": "words", "expected": 42}]},
            image_digests={t["role"]: t["image_digest"] for t in targets},
            config=config,
            vus=1,
            artifact_limit_bytes=config["artifact_limit_bytes"],
            command=(sys.executable, str(generator)),
            graceful_stop_s=0.5,
            request_timeout_s=0.1,
        )
        path = driver.run(workload_root, 6, Event())
    manifest["workload"] = reference(tmp_path, str(path.relative_to(tmp_path)))
    shutil.copyfile(
        workload_root / "workload-inputs.json", tmp_path / "frozen-workload-inputs.json"
    )
    shutil.copyfile(workload_root / "soak-workload.js", tmp_path / "frozen-workload.js")
    manifest["workload_inputs"] = reference(tmp_path, "frozen-workload-inputs.json")
    manifest["workload_script"] = reference(tmp_path, "frozen-workload.js")
    payload = tmp_path / "profile-payload.json"
    payload.write_text(json.dumps({"input": "words", "expected": 42}))
    script = tmp_path / "profile-script.txt"
    script.write_text("synthetic injected sync exercise")
    expected = {
        "images": {t["role"]: t["image_digest"] for t in targets},
        "relevant_config": {
            "sync": {
                "expected_output": 42,
                "roles": config["roles"],
                "workload": config["workload"],
            }
        },
        "payload": describe_artifact(payload),
        "script": describe_artifact(script),
        "settlement": {
            t["role"]: {
                p: {"limit": 0, "retention_s": 0}
                for p in ("live_executions", "payload_bytes", "timers", "pending_http")
            }
            for t in targets
        },
    }
    manifest["prerequisite_inputs"] = save(
        tmp_path, "prerequisite-inputs.json", expected
    )

    class Session:
        async def identities(self):
            return expected["images"]

        async def exercise(self, coverage):
            return {"http_status": 200, "output": 0 if bad_profile else 42}

        async def populations(self):
            return {
                r: dict.fromkeys(policies, 0)
                for r, policies in expected["settlement"].items()
            }

    @asynccontextmanager
    async def runner(coverage, lifetime):
        yield Session()

    writer = ArtifactWriter(tmp_path / "profiles", 100000)
    origin = time.monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(
            prerequisite_module, "monotonic", lambda: 1.0 + time.monotonic() - origin
        )
        prerequisite = asyncio.run(
            prerequisite_module.run_prerequisites(
                inputs=expected,
                required_coverage=frozenset({"sync"}),
                runner=runner,
                writer=writer,
                timeout_s=1,
            )
        )
    writer.close()
    manifest["prerequisites"] = save(tmp_path, "prerequisites.json", prerequisite)
    projection = {
        "schema": SCHEMA,
        "scope": "numerical-projection",
        "purpose": config["purpose"],
        "criteria": config["criteria"],
        "targets": targets,
        "windows": {p: phases[p] for p in ("baseline", "steady", "drain")},
        "sample_interval_s": 1,
        "max_observation_gap_s": 1.1,
        "perturbations": [],
    }
    rows = []
    for phase, window in projection["windows"].items():
        for stamp in range(window["start_s"], window["end_s"] + 1):
            for target in targets:
                for metric in config["roles"][target["role"]]["required_metrics"]:
                    value = (
                        0
                        if metric == "pending"
                        else 150
                        if growth
                        and phase == "drain"
                        and target["role"] == "control-plane"
                        and metric == "process_rss_bytes"
                        else 100
                    )
                    sample = Sample(
                        Target(**target),
                        phase,
                        stamp,
                        stamp,
                        stamp,
                        metric,
                        (),
                        "count" if metric == "pending" else "bytes",
                        value,
                        "observed",
                        "procfs",
                        None,
                    )
                    rows.append({"schema": SCHEMA, **asdict(sample)})
            if phase == "steady":
                sample = Sample(
                    Target(**targets[0]),
                    phase,
                    stamp,
                    stamp,
                    stamp,
                    "function_admitted_total",
                    (("function", "fn"), ("path", "sync")),
                    "count",
                    (stamp - 8) * 2,
                    "observed",
                    "prometheus",
                    None,
                )
                rows.append({"schema": SCHEMA, **asdict(sample)})
    run = {
        "root": tmp_path,
        "manifest": manifest,
        "config": config,
        "targets": targets,
        "preflight": json.loads((tmp_path / manifest["preflight"]["path"]).read_text()),
        "workload": json.loads(path.read_text()),
        "prerequisites": prerequisite,
        "diagnostics": {**binding, "entries": []},
        "projection": projection,
        "samples": rows,
    }
    persist(run)
    if diagnostic or growth:
        receipt, _observation = capture(run, monkeypatch, "final", 20, 150)
        run["diagnostics"]["entries"] = [
            {"role": "control-plane", "operation": "gc", "receipt": receipt}
        ]
        persist(run)
    return run


def capture(run, monkeypatch, name, start, value):
    import nanolab.tasks.soak.diagnostics as module

    root = run["root"]
    target = Target(**run["targets"][0])
    capabilities = root / (name + "-capabilities.json")
    capabilities.write_text(
        json.dumps({"target": asdict(target), "operations": ["gc"]})
    )
    natural = root / (name + "-natural.json")
    natural.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "kind": "natural_checkpoint",
                "completed": True,
                "phase": "drain",
                "target": asdict(target),
                "ended_s": 17,
                "artifacts": [reference(root, "samples.jsonl")],
            }
        )
    )
    ticks = iter(start + i * 0.001 for i in range(1000))

    class Executor:
        def inspect(self, bound, timeout):
            return bound

        def execute(self, request):
            event = {
                "schema": SCHEMA,
                "kind": "full_gc_completed",
                "target": asdict(target),
                "request_id": request.request_id,
                "source": "full-cycle",
                "started_s": request.started_s,
                "ended_s": request.started_s + 0.0001,
            }
            (request.output_dir / "event.json").write_text(json.dumps(event))
            sample = Sample(
                target,
                "diagnostic",
                request.started_s + 0.0002,
                request.started_s + 0.0002,
                request.started_s + 0.0003,
                "process_rss_bytes",
                (),
                "bytes",
                value,
                "observed",
                "procfs",
                None,
            )
            (request.output_dir / "observation.json").write_text(
                json.dumps({"schema": SCHEMA, **asdict(sample)})
            )
            return module.DiagnosticOutcome(0, True, 1, 2, "event.json")

    adapter = module.JvmDiagnosticAdapter(
        module.DiagnosticCapabilities(
            target, HELPER, frozenset({"gc"}), capabilities, True, True, True, True
        ),
        Executor(),
        module.DiagnosticBudget(0, 0, 100000),
        natural_checkpoint=natural,
        max_capture_bytes=10000,
        full_gc_source="full-cycle",
        command_prefix=("jcmd",),
    )
    with monkeypatch.context() as patch:
        patch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        path = adapter.capture(target, "gc", root / name, 2)
    return reference(root, str(path.relative_to(root))), reference(
        root, name + "/artifacts/observation.json"
    )


@pytest.fixture
def complete_run(tmp_path, monkeypatch):
    return make_run(tmp_path, monkeypatch)


def outcome(run, attribution=None):
    return {r.criterion_id: r for r in evaluate_run(run["root"], attribution)}


def test_real_producer_receipts_pass_offline_and_reports_are_immutable(
    complete_run, monkeypatch
):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("offline evaluation attempted live operation")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    first = evaluate_run(complete_run["root"])
    assert combine_results(first, False) == "PASS", first
    assert evaluate_run(complete_run["root"]) == first
    path = write_report(complete_run["root"], first, False)
    body = path.read_bytes()
    second = write_report(complete_run["root"], first, False)
    assert path != second and path.read_bytes() == body
    assert json.loads(body)["p24_qualified"] is True
    payload = json.loads(body)
    assert payload["input_provenance"]["run_id"] == "synthetic-run"
    assert (
        payload["declared_effective_limits"][0]["declared_cpu"]
        == payload["declared_effective_limits"][0]["effective_cpu"]
        == 1
    )


@pytest.mark.parametrize(
    "receipt",
    [
        "config",
        "preflight",
        "workload",
        "prerequisites",
        "diagnostics",
        "artifacts",
        "source",
        "workload_inputs",
        "workload_script",
        "prerequisite_inputs",
    ],
)
def test_missing_receipt_never_passes(complete_run, receipt):
    run = complete_run
    (run["root"] / run["manifest"][receipt]["path"]).unlink()
    assert combine_results(tuple(outcome(run).values()), False) == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "change", ["per-function", "aggregate", "source", "unavailable", "raw-summary"]
)
def test_workload_conservation_and_source_are_independent_of_flags(
    complete_run, change
):
    run = complete_run
    if change == "per-function":
        run["workload"]["per_function"]["fn"]["offered"]["value"] = 13
    elif change == "aggregate":
        run["workload"]["counters"]["success"]["value"] = 11
    elif change == "source":
        run["workload"]["counters"]["offered"]["source"] = "caller says PASS"
    elif change == "unavailable":
        run["workload"]["per_function"]["fn"]["error"]["availability"] = "unavailable"
    else:
        path = run["root"] / "load/k6-summary.json"
        summary = json.loads(path.read_text())
        summary["metrics"]["soak_success"]["values"]["count"] = 1
        path.write_text(json.dumps(summary))
    run["workload"]["completed"] = True
    persist(run)
    assert outcome(run)["workload-accounting"].status == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "change", ["missing", "wrong-role", "wrong-source", "reset", "gap", "success-count"]
)
def test_admission_never_comes_from_a_client_claim(complete_run, change):
    run = complete_run
    rows = [r for r in run["samples"] if r["metric"] == "function_admitted_total"]
    if change == "missing":
        run["samples"] = [r for r in run["samples"] if r not in rows]
    elif change == "wrong-role":
        rows[-1]["target"] = run["targets"][1]
    elif change == "wrong-source":
        rows[-1]["source"] = "client-http-success"
    elif change == "reset":
        rows[-1]["value"] = 0
    elif change == "gap":
        run["samples"].remove(rows[-1])
    else:
        rows[-1]["value"] = 11
    run["workload"]["counters"]["admitted"] = {
        "value": 12,
        "availability": "observed",
        "source": "server",
    }
    persist(run)
    assert outcome(run)["workload-accounting"].status == "INCONCLUSIVE"


@pytest.mark.parametrize("change", ["schema", "caller-pass", "input", "raw-evidence"])
def test_prerequisites_replay_producer_evidence(complete_run, change):
    run = complete_run
    if change == "schema":
        run["prerequisites"]["schema"] = "unsupported"
    elif change == "caller-pass":
        run["prerequisites"]["profiles"][0]["assertions"] = []
        run["prerequisites"]["status"] = "PASS"
    elif change == "input":
        run["prerequisites"]["inputs"]["relevant_config"]["sync"]["expected_output"] = (
            99
        )
    else:
        Path(run["prerequisites"]["profiles"][0]["evidence"]["path"]).write_text(
            '{"kind":"profile","status":"PASS"}\n'
        )
    persist(run)
    assert outcome(run)["prerequisite-coverage"].status == "INCONCLUSIVE"


def test_real_prerequisite_failure_survives_absence_and_abort(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch, bad_profile=True)
    (run["root"] / "load/k6-summary.json").unlink()
    first = outcome(run)
    assert first["prerequisite-coverage"].status == "FAIL"
    assert combine_results(tuple(first.values()), False) == "FAIL"
    run["manifest"]["aborted"] = True
    run["manifest"]["completed"] = False
    persist(run)
    final = outcome(run)
    assert final["prerequisite-coverage"].status == "FAIL"
    assert combine_results(tuple(final.values()), False) == "ABORTED"


@pytest.mark.parametrize(
    "change",
    [
        "source-tree",
        "build-log",
        "recipe",
        "cpu",
        "short",
        "missing-metric",
        "restart",
        "release",
    ],
)
def test_provenance_protocol_and_release_boundaries(complete_run, change):
    run = complete_run
    if change == "source-tree":
        (run["root"] / "source/tree/input.txt").write_text("different source")
    elif change == "build-log":
        (run["root"] / "build/observed-fn.json").write_text("different build")
    elif change == "recipe":
        (run["root"] / "build/recipe-fn.json").write_text("{}")
    elif change == "cpu":
        run["preflight"]["observations"]["roles"]["fn"]["cpu"] = 2
    elif change == "short":
        run["manifest"]["phases"]["drain"]["end_s"] = 16
    elif change == "missing-metric":
        run["samples"] = [r for r in run["samples"] if r["metric"] != "pending"]
    elif change == "restart":
        run["manifest"]["restarts"] = [{"role": "fn", "at_s": 12}]
    else:
        (run["root"] / "load/generator-process.json").write_text(
            json.dumps({"descendants_reaped": False})
        )
    persist(run)
    status = combine_results(tuple(outcome(run).values()), False)
    assert status == ("FAIL" if change == "restart" else "INCONCLUSIVE")


def test_smoke_real_receipts_cannot_qualify_p24(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch, smoke=True)
    results = evaluate_run(run["root"])
    assert combine_results(results, False) == "PASS", results
    assert (
        json.loads(write_report(run["root"], results, False).read_text())[
            "p24_qualified"
        ]
        is False
    )


def test_real_diagnostic_producer_receipt_passes(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch, diagnostic=True)
    assert outcome(run)["diagnostic-coverage"].status == "PASS"
    assert combine_results(tuple(outcome(run).values()), False) == "PASS"


@pytest.mark.parametrize("change", ["event", "target", "helper", "natural", "time"])
def test_diagnostic_receipt_cannot_replace_completion_proof(
    tmp_path, monkeypatch, change
):
    run = make_run(tmp_path, monkeypatch, diagnostic=True)
    ref = run["diagnostics"]["entries"][0]["receipt"]
    path = run["root"] / ref["path"]
    data = json.loads(path.read_text())
    if change == "event":
        (path.parent / "artifacts/event.json").write_text("{}")
    elif change == "target":
        data["target"]["process_id"] = 99
    elif change == "helper":
        data["helper_digest"] = "unknown"
    elif change == "natural":
        Path(data["natural_checkpoint"]["path"]).write_text("{}")
    else:
        data["ended_s"] = data["started_s"] - 1
    run["diagnostics"]["entries"][0]["receipt"] = save(run["root"], ref["path"], data)
    persist(run)
    assert outcome(run)["diagnostic-coverage"].status == "INCONCLUSIVE"


def attribution(run, monkeypatch):
    import nanolab.tasks.soak.workload as module

    def traffic(name, start):
        class VirtualClockRunner(module.OwnedCommandRunner):
            def run(self):
                return replace(super().run(), ended_s=start + 6)

        with monkeypatch.context() as patch:
            patch.setattr(module, "time", SimpleNamespace(monotonic=lambda: start))
            patch.setattr(module, "OwnedCommandRunner", VirtualClockRunner)
            driver = module.K6WorkloadDriver(
                base_url="http://127.0.0.1:1",
                function_rates={"fn": 2},
                payloads={"fn": [{"input": "words", "expected": 42}]},
                image_digests={t["role"]: t["image_digest"] for t in run["targets"]},
                config=run["config"],
                vus=1,
                artifact_limit_bytes=run["config"]["artifact_limit_bytes"],
                command=(sys.executable, str(run["root"] / "fake-k6.py")),
                graceful_stop_s=0.5,
                request_timeout_s=0.1,
            )
            path = driver.run(run["root"] / name, 6, Event())
        records = []
        for stamp in range(start, start + 7):
            row = Sample(
                Target(**run["targets"][0]),
                "steady",
                stamp,
                stamp,
                stamp,
                "function_admitted_total",
                (("function", "fn"), ("path", "sync")),
                "count",
                (stamp - start) * 2,
                "observed",
                "prometheus",
                None,
            )
            records.append({"schema": SCHEMA, **asdict(row)})
        sample_path = run["root"] / (name + "-admission.jsonl")
        sample_path.write_text("".join(json.dumps(row) + "\n" for row in records))
        return reference(run["root"], str(path.relative_to(run["root"]))), reference(
            run["root"], sample_path.name
        )

    traffic_first, admission_first = traffic("traffic-first", 30)
    traffic_second, admission_second = traffic("traffic-second", 40)
    first, first_sample = capture(run, monkeypatch, "equal-first", 36.2, 150)
    second, second_sample = capture(run, monkeypatch, "equal-second", 46.2, 152)
    binding = {k: run["manifest"][k] for k in ("schema", "run_id", "policy_sha256")}
    work = {
        **binding,
        "criterion_id": "control-plane.rss",
        "metric": "process_rss_bytes",
        "unit": "bytes",
        "target": run["targets"][0],
        "windows": [
            {
                "started_s": 30,
                "ended_s": 37,
                "offered": 12,
                "admitted": 12,
                "workload": traffic_first,
                "admission_samples": admission_first,
                "post_gc": first,
                "observation": first_sample,
            },
            {
                "started_s": 40,
                "ended_s": 47,
                "offered": 12,
                "admitted": 12,
                "workload": traffic_second,
                "admission_samples": admission_second,
                "post_gc": second,
                "observation": second_sample,
            },
        ],
    }
    ref = save(run["root"], "equal-work.json", work)
    analysis = save(
        run["root"],
        "owner-analysis.json",
        {"owner": "payloads", "population": "bounded resident state"},
    )
    record = {
        "schema": SCHEMA,
        "criterion_id": "control-plane.rss",
        "status": "resolved",
        "owner": "payloads",
        "population": "bounded resident state",
        "expected_lifetime_s": 2,
        "remaining_bytes": 152,
        "budget_bytes": 200,
        "rationale": "observed bounded owner",
        "reviewer": "synthetic reviewer",
        "policy_override": False,
        "policy_artifact": run["manifest"]["config"],
        "artifacts": [analysis],
    }
    document = {**binding, "entries": [{"attribution": record, "equal_work": ref}]}
    save(run["root"], "attribution.json", document)
    persist(run)
    return document, run["root"] / "attribution.json"


def test_positive_attribution_uses_real_gc_receipts_and_post_gc_samples(
    tmp_path, monkeypatch
):
    run = make_run(tmp_path, monkeypatch, growth=True)
    assert outcome(run)["control-plane.rss"].status == "INCONCLUSIVE"
    _document, path = attribution(run, monkeypatch)
    results = outcome(run, path)
    assert results["attribution-policy-binding"].status == "PASS", results
    assert results["control-plane.rss"].status == "PASS"
    assert combine_results(tuple(results.values()), False) == "PASS"


@pytest.mark.parametrize(
    "change",
    [
        "unknown-owner",
        "policy",
        "unequal-work",
        "unobserved",
        "budget",
        "numeric-failure",
    ],
)
def test_attribution_cannot_waive_missing_proof_or_frozen_limits(
    tmp_path, monkeypatch, change
):
    run = make_run(tmp_path, monkeypatch, growth=True)
    document, path = attribution(run, monkeypatch)
    record = document["entries"][0]["attribution"]
    if change == "unknown-owner":
        record["owner"] = "runtime"
    elif change == "policy":
        record["policy_override"] = True
    elif change == "unequal-work":
        work = json.loads((run["root"] / "equal-work.json").read_text())
        work["windows"][1]["admitted"] = 11
        document["entries"][0]["equal_work"] = save(
            run["root"], "equal-work.json", work
        )
    elif change == "unobserved":
        (run["root"] / "equal-second/artifacts/observation.json").write_text("{}")
    elif change == "budget":
        record["budget_bytes"] = 500
    else:
        for row in run["samples"]:
            if (
                row["metric"] == "cgroup_memory_usage_bytes"
                and row["phase"] == "steady"
            ):
                row["value"] = 2000
    save(run["root"], "attribution.json", document)
    persist(run)
    results = outcome(run, path)
    assert combine_results(tuple(results.values()), False) == (
        "FAIL" if change == "numeric-failure" else "INCONCLUSIVE"
    )


@pytest.mark.parametrize("aggregate", [False, True])
def test_conservation_is_recomputed_even_when_receipt_matches_raw_summary(
    complete_run, aggregate
):
    run = complete_run
    path = run["root"] / "load/k6-summary.json"
    summary = json.loads(path.read_text())
    if aggregate:
        for key in ("offered", "success"):
            run["workload"]["counters"][key]["value"] = 13
            summary["metrics"]["soak_" + key]["values"]["count"] = 13
    else:
        run["workload"]["per_function"]["fn"]["offered"]["value"] = 13
        summary["metrics"]["soak_offered{scenario:fn_0}"]["values"]["count"] = 13
    path.write_text(json.dumps(summary))
    persist(run)
    result = outcome(run)["workload-accounting"]
    assert result.status == "INCONCLUSIVE"
    assert (
        "per-function sum" if aggregate else "offered != success + error"
    ) in result.reason


def test_phase_alias_is_explicit_and_duplicates_are_rejected(complete_run):
    from nanolab.tasks.soak.acceptance import normalize_phase_windows

    run = complete_run
    phases = run["manifest"]["phases"]
    phases["baseline-drain"] = phases.pop("baseline_drain")
    persist(run)
    assert outcome(run)["run-continuity"].status == "PASS"
    with pytest.raises(ValueError, match="duplicate phase alias"):
        normalize_phase_windows({"baseline_drain": {}, "baseline-drain": {}})


@pytest.mark.parametrize(
    "field", ["quota_exceeded", "cleanup_complete", "output_limit_bytes"]
)
def test_receipt_success_cannot_hide_quota_or_release_violation(complete_run, field):
    run = complete_run
    run["workload"][field] = {
        "quota_exceeded": True,
        "cleanup_complete": False,
        "output_limit_bytes": 0,
    }[field]
    run["workload"]["completed"] = True
    persist(run)
    assert outcome(run)["workload-accounting"].status == "INCONCLUSIVE"


def test_equal_work_requires_actual_admission_artifact(tmp_path, monkeypatch):
    run = make_run(tmp_path, monkeypatch, growth=True)
    _document, path = attribution(run, monkeypatch)
    (run["root"] / "traffic-second-admission.jsonl").write_text(
        "caller says admitted=12\n"
    )
    result = outcome(run, path)["attribution-policy-binding"]
    assert result.status == "INCONCLUSIVE"


def test_admission_scrape_cannot_cross_actual_traffic_boundary(complete_run):
    run = complete_run
    for row in run["samples"]:
        if row["metric"] == "function_admitted_total" and row["scheduled_s"] == 14:
            row["ended_s"] = 15
    persist(run)
    assert outcome(run)["workload-accounting"].status == "INCONCLUSIVE"


@pytest.mark.parametrize("offered", [3, 20])
def test_completed_flag_cannot_hide_gross_under_or_overdelivery(complete_run, offered):
    run = complete_run
    path = run["root"] / "load/k6-summary.json"
    summary = json.loads(path.read_text())
    for group in (run["workload"]["counters"], run["workload"]["per_function"]["fn"]):
        for key in ("offered", "success"):
            group[key]["value"] = offered
    for suffix in ("", "{scenario:fn_0}"):
        for key in ("offered", "success"):
            summary["metrics"]["soak_" + key + suffix]["values"]["count"] = offered
    path.write_text(json.dumps(summary))
    state_path = run["root"] / "load/generator-process.json"
    state = json.loads(state_path.read_text())
    state["summary_bytes"] = path.stat().st_size
    state_path.write_text(json.dumps(state))
    run["workload"]["summary_bytes"] = state["summary_bytes"]
    for row in run["samples"]:
        if row["metric"] == "function_admitted_total":
            row["value"] = int((row["scheduled_s"] - 8) * offered / 6)
    persist(run)
    result = outcome(run)["workload-accounting"]
    assert result.status == "INCONCLUSIVE"
    assert "scheduled demand" in result.reason


def test_two_function_producer_matches_global_allocation(complete_run, monkeypatch):
    import nanolab.tasks.soak.workload as module
    from nanolab.tasks.soak.acceptance import _workload_evidence

    run = complete_run
    raw = json.loads(json.dumps(run["config"]))
    raw["workload"].update(rates={"fn": 2, "fn2": 2}, preallocated_vus=2, max_vus=4)
    raw["roles"]["fn2"] = dict(raw["roles"]["fn"])
    raw["images"]["fn2"] = dict(raw["images"]["fn"])
    raw["criteria"].extend(
        {**criterion, "id": criterion["id"] + ".second", "role": "fn2"}
        for criterion in list(raw["criteria"])
        if criterion["role"] == "fn"
    )
    config = SoakConfig.model_validate(raw)
    targets = {target["role"]: target for target in run["targets"]}
    targets["fn2"] = {
        **targets["fn"],
        "role": "fn2",
        "container_id": "fn2",
        "process_id": 3,
        "image_digest": "fn2@sha256:" + "a" * 64,
    }

    class VirtualClockRunner(module.OwnedCommandRunner):
        def run(self):
            return replace(super().run(), ended_s=14.0)

    with monkeypatch.context() as patch:
        patch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 8.0))
        patch.setattr(module, "OwnedCommandRunner", VirtualClockRunner)
        driver = module.K6WorkloadDriver(
            base_url="http://127.0.0.1:1",
            function_rates=config.workload.rates,
            payloads={
                name: [{"input": "words", "expected": 42}]
                for name in config.workload.rates
            },
            image_digests={
                name: target["image_digest"] for name, target in targets.items()
            },
            config=config.model_dump(mode="json"),
            vus=2,
            max_vus=4,
            artifact_limit_bytes=config.artifact_limit_bytes,
            command=(sys.executable, str(run["root"] / "fake-k6.py")),
            graceful_stop_s=0.5,
            request_timeout_s=0.1,
        )
        path = driver.run(run["root"] / "two-functions", 6, Event())
    scoped = {
        **run["manifest"],
        "workload": reference(run["root"], str(path.relative_to(run["root"]))),
        "workload_inputs": reference(run["root"], "two-functions/workload-inputs.json"),
    }
    receipt, counters, _ = _workload_evidence(run["root"], scoped, config, targets)
    assert receipt["completed"] is True
    assert receipt["vu_policy"]["allocation"] == module.allocate_vus(
        config.workload.rates, 2, 4
    )
    assert sum(counts["offered"] for counts in counters.values()) == 24
    effective_path = path.parent / "workload-config.json"
    effective = json.loads(effective_path.read_text())
    for function in effective["functions"]:
        function["options"].update(preAllocatedVUs=2, maxVUs=4)
    effective_path.write_text(json.dumps(effective))
    receipt["provenance"]["effective_config_sha256"] = fingerprint(effective)
    scoped["workload"] = save(run["root"], str(path.relative_to(run["root"])), receipt)
    with pytest.raises(ValueError, match=r"effective constant workload differs"):
        _workload_evidence(run["root"], scoped, config, targets)


def test_prerequisite_exemption_only_passes_under_explicit_smoke_policy():
    """A policy that requires coverage is never satisfied by an exemption."""
    from types import SimpleNamespace

    from nanolab.tasks.soak.acceptance import exemption_is_allowed

    receipt = {
        "schema": SCHEMA,
        "kind": "prerequisite-exemption",
        "purpose": "smoke",
        "coverage": [],
        "p24_qualified": False,
    }

    def policy(purpose, coverage) -> SoakConfig:
        return cast(
            SoakConfig,
            SimpleNamespace(
                purpose=purpose,
                prerequisites=SimpleNamespace(required_coverage=coverage),
            ),
        )

    assert exemption_is_allowed(receipt, policy("smoke", []))
    # The requirement it would be standing in for.
    assert not exemption_is_allowed(receipt, policy("smoke", ["sync"]))
    # P24 never exempts itself, even with nothing declared.
    assert not exemption_is_allowed(receipt, policy("p24", []))
    # A receipt claiming qualification is not an exemption.
    assert not exemption_is_allowed(
        {**receipt, "p24_qualified": True}, policy("smoke", [])
    )
    assert not exemption_is_allowed(
        {**receipt, "coverage": ["sync"]}, policy("smoke", [])
    )


def test_phase_contiguity_allows_the_transition_cost_but_not_a_lost_tick():
    """Consecutive phases are timed by separate clock reads, never bit-identical.

    Requiring exact equality could not pass any real run. What must hold is
    that no sampling tick can fall into an unattributed gap between phases.
    """
    from nanolab.tasks.soak.acceptance import phases_are_contiguous

    assert phases_are_contiguous(start=100.0, previous=100.0, sample_interval_s=3)
    # The few milliseconds a real transition costs.
    assert phases_are_contiguous(start=100.004, previous=100.0, sample_interval_s=3)
    # A gap wide enough to have swallowed a scheduled sample.
    assert not phases_are_contiguous(start=103.5, previous=100.0, sample_interval_s=3)
    # Phases must not overlap or run backwards.
    assert not phases_are_contiguous(start=99.9, previous=100.0, sample_interval_s=3)


def test_run_timing_order_matches_how_the_run_actually_records_it():
    """Images are frozen after their builds finish, not before.

    The gate asserted the opposite, so no real run could satisfy it.
    """
    from nanolab.tasks.soak.preparation import PreparedSoak

    order = [f.name for f in dataclasses.fields(PreparedSoak)]
    assert order.index("builds_finished_s") < order.index("frozen_at_s")


def test_required_sample_count_is_what_an_unaligned_window_can_actually_yield():
    """Ticks do not line up with phase boundaries, and durations are float reads.

    ceil() over a 30.0000001 s window at 3 s demanded 11 samples where 10 is
    the most an unaligned window guarantees, failing a perfectly sampled run.
    """
    from nanolab.tasks.soak.acceptance import required_sample_count

    assert required_sample_count(30.0, 3) == 10
    # The same window, measured a hair long by two clock reads.
    assert required_sample_count(30.000000123, 3) == 10
    assert required_sample_count(120.0, 10) == 12
    # Never fewer than two, so a short window still needs a real series.
    assert required_sample_count(1.0, 10) == 2


def test_admission_boundaries_bracket_the_window_within_one_interval():
    """Sampling ticks have their own origin and never land on a phase boundary.

    Requiring a sample scheduled at exactly the window edge could not pass;
    what matters is that the first and last observations bracket the window
    closely enough to account for the whole interval.
    """
    from nanolab.tasks.soak.acceptance import brackets_window

    assert brackets_window(first=100.0, last=160.0, low=100.0, high=160.0, slack=3)
    # Ticks just inside each edge, as a real run produces.
    assert brackets_window(first=100.4, last=158.4, low=100.0, high=160.0, slack=3)
    # A whole missing observation sits exactly one interval from the edge.
    assert not brackets_window(first=100.0, last=157.0, low=100.0, high=160.0, slack=3)
    # A first observation too late to account for the start of the window.
    assert not brackets_window(first=105.0, last=158.4, low=100.0, high=160.0, slack=3)
    # A last observation too early to account for its end.
    assert not brackets_window(first=100.4, last=150.0, low=100.0, high=160.0, slack=3)
    # Observations outside the window are not boundaries of it.
    assert not brackets_window(first=99.0, last=158.4, low=100.0, high=160.0, slack=3)


def test_admission_reconciliation_allows_only_the_unticked_edges():
    """The counter delta misses traffic before the first tick and after the last.

    Requiring success <= admitted could not hold: at 1/s with ~3 s of window
    outside the two readings, three requests are legitimately uncounted. A
    real shortfall of admitted work must still fail.
    """
    from nanolab.tasks.soak.acceptance import admission_is_consistent

    # 60 offered, all succeeded, 57 seen between the boundary readings.
    assert admission_is_consistent(
        admitted=57, success=60, offered=60, rate=1.0, uncounted_s=3.08
    )
    # Work that actually never reached the platform.
    assert not admission_is_consistent(
        admitted=30, success=60, offered=60, rate=1.0, uncounted_s=3.08
    )
    # The server cannot have admitted more than was offered.
    assert not admission_is_consistent(
        admitted=61, success=60, offered=60, rate=1.0, uncounted_s=3.08
    )
    # No offered work is never consistent.
    assert not admission_is_consistent(
        admitted=0, success=0, offered=0, rate=1.0, uncounted_s=3.08
    )
    # A tightly bracketed window forgives nothing at all.
    assert not admission_is_consistent(
        admitted=59, success=60, offered=60, rate=1.0, uncounted_s=0.0
    )
