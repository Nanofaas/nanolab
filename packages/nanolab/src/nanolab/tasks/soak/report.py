"""Immutable numerical and full-run JSON/Markdown assessments."""

import html
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nanolab.tasks.soak.acceptance import GATE_IDS, _json, _path, _reference
from nanolab.tasks.soak.artifacts import ArtifactWriter, describe_artifact
from nanolab.tasks.soak.evaluate import combine_results
from nanolab.tasks.soak.models import CriterionResult


def _cell(value: object) -> str:
    return (
        html.escape(str(value))
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def write_report(
    run_dir: Path, results: tuple[CriterionResult, ...], aborted: bool
) -> Path:
    """Publish a fresh assessment, preserving prior reports and source evidence."""
    if len(results) > 1100:
        raise ValueError("report criterion count limit exceeded")
    purpose = "unknown"
    roles: dict[str, str] = {}
    warnings = []
    try:
        source = run_dir / "evaluation-input.json"
        if source.is_symlink():
            raise ValueError("symlink report input is unsupported")
        with source.open("rb") as stream:
            body = stream.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("report input limit exceeded")
        projection = json.loads(body)
        if projection.get("schema") != "nanolab-soak-v1":
            raise ValueError("unsupported report input schema")
        if projection.get("purpose") in ("smoke", "p24"):
            purpose = projection["purpose"]
        roles = {item["id"]: item["role"] for item in projection.get("criteria", [])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        warnings.append("Report metadata unavailable: " + str(error)[:512])
    parent = run_dir / "evaluations"
    if parent.is_symlink():
        raise ValueError("symlink evaluation directory is unsupported")
    parent.mkdir(exist_ok=True)
    output = parent / ("evaluation-" + uuid4().hex)
    writer = ArtifactWriter(output, 8 * 1024 * 1024)
    status = combine_results(results, aborted)
    gates = {
        item.criterion_id: item for item in results if item.criterion_id in GATE_IDS
    }
    full = GATE_IDS.issubset(gates)
    input_provenance = {}
    limits = []
    if full:
        try:
            manifest_path = _path(run_dir, "acceptance-manifest.json")
            manifest = _json(manifest_path)
            input_provenance = {
                "acceptance_manifest": describe_artifact(manifest_path),
                "evaluation_input": describe_artifact(
                    _path(run_dir, "evaluation-input.json")
                ),
                "run_id": manifest["run_id"],
                "policy_sha256": manifest["policy_sha256"],
            }
            preflight_path = _reference(run_dir, manifest["preflight"], 1024 * 1024)
            preflight = _json(preflight_path)
            for role, declared in preflight["configuration"]["roles"].items():
                observed = preflight["observations"]["roles"].get(role, {})
                limits.append(
                    {
                        "role": role,
                        "declared_cpu": declared["expected_cpu"],
                        "effective_cpu": observed.get("cpu"),
                        "declared_memory_bytes": declared["memory_limit_bytes"],
                        "effective_memory_bytes": observed.get("memory_bytes"),
                        "runtime": observed.get("runtime"),
                        "sources": observed.get("limit_sources"),
                    }
                )
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            warnings.append("Bound report provenance unavailable: " + str(error)[:512])
            if status == "PASS":
                status = "INCONCLUSIVE"
    qualified = full and status == "PASS" and purpose == "p24" and not warnings
    unverified = [
        item.criterion_id + ": " + item.reason
        for item in results
        if item.status == "INCONCLUSIVE"
    ]
    if not full:
        unverified.append("full-run acceptance receipts have not all been verified")
    payload = {
        "schema": "nanolab-soak-v1",
        "scope": "full-run" if full else "numerical-only",
        "purpose": purpose,
        "status": status,
        "p24_qualified": qualified,
        "aborted": status == "ABORTED",
        "results": [asdict(item) for item in results],
        "warnings": warnings,
        "input_provenance": input_provenance,
        "declared_effective_limits": limits,
        "unverified": unverified,
        "next_action": (
            "Inspect failed criteria and collect missing bound evidence; "
            "do not widen frozen limits."
        )
        if status != "PASS"
        else (
            "Preserve this run's evidence; "
            "this result does not close the entire P24 campaign."
        ),
    }
    lines = [
        "# Soak full-run assessment" if full else "# Soak numerical assessment",
        "",
        f"Status: **{status}**",
        f"Purpose: {_cell(purpose)}",
        f"P24 qualified: **{str(qualified).lower()}**",
        "",
        (
            "Acceptance applies only to the recorded profile and duration, "
            "not the entire P24 campaign."
        )
        if full
        else "This report is numerical-only, not complete soak acceptance.",
        "Natural RSS recovery is evaluated separately from heap "
        "and diagnostic observations.",
        "",
        "| Role | Criterion | Status | Evidence / reason |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            _cell(value)
            for value in (
                roles.get(item.criterion_id, "run"),
                item.criterion_id,
                item.status,
                item.reason + " [" + ", ".join(item.evidence) + "]",
            )
        )
        + " |"
        for item in results
    )
    if limits:
        lines += [
            "",
            "## Declared and effective limits",
            "",
            "| Role | Declared CPU | Effective CPU | Declared memory bytes "
            "| Effective memory bytes | Runtime |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in limits:
            lines.append(
                "| "
                + " | ".join(
                    _cell(item[key])
                    for key in (
                        "role",
                        "declared_cpu",
                        "effective_cpu",
                        "declared_memory_bytes",
                        "effective_memory_bytes",
                        "runtime",
                    )
                )
                + " |"
            )
    lines += [
        "",
        "## Remaining validation",
        "",
        *[_cell(item) for item in unverified],
        payload["next_action"],
    ]
    markdown = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        try:
            if len(markdown) > 1024 * 1024:
                raise ValueError("Markdown report limit exceeded")
            with (output / "report.md").open("xb") as stream:
                stream.write(markdown)
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError) as error:
            warnings.append(
                "Markdown unavailable; JSON remains authoritative: " + str(error)[:512]
            )
        return writer.write_json("report.json", payload)
    finally:
        writer.close()
