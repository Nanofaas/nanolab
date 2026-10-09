"""Local packaged contracts and a qualification boundary after all releases."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sonata_engine import Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.functions.contracts import ContractCase, resolve_contract_matrix, same_json
from nanolab.tasks.recipes.workflow import recipe_run_resource
from nanolab.tasks.validation.contract_resources import (
    contract_images_resource,
    contract_runtime_resource,
    host_architecture,
    verify_contract_cleanup,
)
from nanolab.tasks.validation.function_contracts import (
    FunctionContractsTask,
    check_contract_logs,
    validate_case_observation,
)

EXCLUSIONS = (
    "Other families, fixtures and service images have no approved independent corpus.",
    "Rust, cross-architecture execution and watchdog FILE/HTTP supervision "
    "are excluded.",
    "QR qualification checks PNG structure and dimensions, not decoded QR text.",
    "Control-plane and CLI qualification are separate workflows.",
)


def build_contract_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    repo_root: Path,
    environment: EnvironmentConfig,
    run_dir: Path,
    scenario_path: Path,
) -> Workflow:
    """Construct the complete local graph without acquiring platform resources."""
    if config.workflow != "contract" or config.contract is None:
        raise ValueError("contract plan requires a contract scenario")
    if environment.provider != "local":
        raise ValueError("contract requires a local environment")
    tag = uuid4().hex
    selectors = tuple(config.functions)
    resolve_contract_matrix(
        repo_root, selectors, architecture=host_architecture(), tag=tag
    )
    executor = RoleBoundCommandTaskExecutor(bindings)
    source = recipe_run_resource(
        source=repo_root, recipe=scenario_path, run_dir=run_dir / "inputs", tag=tag
    )
    images = contract_images_resource(
        source,
        selectors=selectors,
        settings=config.contract,
        executor=executor,
        run_dir=run_dir,
        tag=tag,
    )
    runtime = contract_runtime_resource(
        settings=config.contract,
        executor=executor,
        run_dir=run_dir,
        tag=tag,
        requires=(images,),
    )
    workflow = Workflow(workflow_id="contract")
    workflow.add(
        FunctionContractsTask(
            source,
            images,
            runtime,
            selectors=selectors,
            settings=config.contract,
            executor=executor,
            run_dir=run_dir,
        ),
        requires=(source, images, runtime),
    )
    return workflow


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"invalid contract receipt: {path}")
    return cast(dict[str, Any], value)


def finalize_contract_run(run_dir: Path, *, executor: CommandTaskExecutor) -> Path:
    """Revalidate complete immutable cases, then independently prove cleanup."""
    marker = run_dir / "qualification.json"
    if marker.exists():
        raise FileExistsError(marker)
    matrix = _read(run_dir / "matrix.json")
    index = _read(run_dir / "case-index.json")
    if index.get("status") != "passed" or index.get("audited") is not True:
        raise ValueError("contract final audit did not pass")
    owned = []
    for path in (run_dir / "ownership").glob("*.json"):
        item = _read(path)
        if item["identity"] is None:
            item["identity"] = _read(run_dir / "identity-bindings" / path.name)[
                "identity"
            ]
        if not item["identity"]:
            raise ValueError("contract resource identity missing")
        owned.append(item)
    identities = {
        kind: {item["identity"] for item in owned if item["kind"] == kind}
        for kind in ("image", "container", "network")
    }
    if not all(identities.values()):
        raise ValueError("contract ownership inventory incomplete")
    runtime = index["runtime"]
    if runtime["capture_container"] not in identities["container"] or not any(
        item["kind"] == "network" and item["reference"] == runtime["network"]
        for item in owned
    ):
        raise ValueError("contract capture/network ownership missing")
    helper = _read(run_dir / "capture-image.json")
    if helper["Id"] not in identities["image"]:
        raise ValueError("contract helper image ownership missing")
    audit_entry = index["finalAudit"]
    audit_path = run_dir / audit_entry["path"]
    if not audit_path.resolve().is_relative_to(run_dir.resolve()) or (
        hashlib.sha256(audit_path.read_bytes()).hexdigest() != audit_entry["sha256"]
    ):
        raise ValueError("contract final audit identity changed")
    audit = _read(audit_path)
    if audit["violations"] or any(
        audit.get(key) != 0 for key in ("activeCallbacks", "activeRequests")
    ):
        raise ValueError("contract final audit has violations or pending requests")
    capture_records = {}
    for record in audit["records"]:
        seq = record["sequence"]
        raw_path = run_dir / "capture" / f"{seq:04d}.raw"
        raw = raw_path.read_bytes()
        metadata = _read(raw_path.with_suffix(".json"))
        if (
            hashlib.sha256(raw).hexdigest() != record["bodySha256"]
            or raw != base64.b64decode(record["bodyBase64"], validate=True)
            or metadata != {k: v for k, v in record.items() if k != "bodyBase64"}
            or record["executionId"] in capture_records
        ):
            raise ValueError("contract capture evidence changed or duplicated")
        capture_records[record["executionId"]] = record
    settings = _read(run_dir / "contract-settings.json")
    check_contract_logs(
        (run_dir / "capture.log").read_bytes(), limit=settings["log_bytes"]
    )
    expected: dict[str, tuple[ContractCase, Literal["sdk", "warm", "one-shot"]]] = {}
    image_ids: dict[str, str] = {}
    counts = {"http": 0, "oneShot": 0, "callbacks": 0}
    for cell_number, cell in enumerate(matrix["images"]):
        image = _read(run_dir / "images" / f"{cell_number:03d}.json")
        if image["cell"] != cell:
            raise ValueError("contract image matrix changed")
        image_id = image["image"]["Id"]
        if image_id not in identities["image"]:
            raise ValueError("contract image ownership missing")
        name = cell["target"]["name"]
        family = next(f for f in matrix["cases"] if name.endswith("-" + f))
        bash = name.startswith("bash-")
        modes: tuple[Literal["sdk", "warm", "one-shot"], ...] = (
            ("warm", "one-shot") if bash else ("sdk",)
        )
        for mode in modes:
            for number, value in enumerate(matrix["cases"][family]):
                identifier = f"{name}-{cell['flavor']}/{mode}/{number:03d}"
                if identifier in expected:
                    raise ValueError("duplicate required contract case")
                expected[identifier] = ContractCase(**value), mode
                image_ids[identifier] = image_id
                counts["oneShot" if mode == "one-shot" else "http"] += 1
                counts["callbacks"] += mode != "warm"
    if not expected or any(
        index.get(key) != counts[count]
        for key, count in (
            ("expectedHttp", "http"),
            ("expectedOneShot", "oneShot"),
            ("expectedCallbacks", "callbacks"),
        )
    ):
        raise ValueError("contract derived counts disagree")
    observed, executions = set(), set()
    receipts = []
    for item in index["cases"]:
        path = run_dir / item["path"]
        if not path.resolve().is_relative_to(run_dir.resolve()):
            raise ValueError("contract receipt escapes attempt")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("contract receipt hash changed")
        receipt = _read(path)
        identifier = receipt["id"]
        if identifier not in expected or identifier in observed:
            raise ValueError("unexpected or duplicate contract receipt")
        case, mode = expected[identifier]
        if receipt.get("imageId") != image_ids[identifier]:
            raise ValueError("contract artifact image identity changed")
        if (
            receipt.get("status") != "passed"
            or receipt.get("error") is not None
            or not same_json(receipt["case"], asdict(case))
        ):
            raise ValueError("contract case receipt did not pass frozen oracle")
        execution = receipt["executionId"]
        if not execution or execution in executions:
            raise ValueError("duplicate contract execution identity")
        if receipt["http"] is not None:
            http = receipt["http"]
            if http.get("executionId") != execution or (
                path.parent / "response.raw"
            ).read_bytes() != base64.b64decode(http["bodyBase64"], validate=True):
                raise ValueError("contract raw response identity changed")
        if any(cb.get("executionId") != execution for cb in receipt["callbacks"]):
            raise ValueError("contract callback execution identity changed")
        validate_case_observation(
            case,
            mode=mode,
            http=receipt["http"],
            callbacks=tuple(receipt["callbacks"]),
            exit_code=receipt["exitCode"],
        )
        observed.add(identifier)
        executions.add(execution)
        container_id = receipt["containerId"]
        if container_id not in identities["container"]:
            raise ValueError("contract artifact container ownership missing")
        container = _read(run_dir / "containers" / f"{container_id}.json")
        if container["Id"] != container_id or container["Image"] != receipt["imageId"]:
            raise ValueError("contract artifact container evidence changed")
        check_contract_logs(
            (run_dir / "container-logs" / f"{container_id}.log").read_bytes(),
            limit=settings["log_bytes"],
        )
        actual = capture_records.pop(execution, None)
        if (mode == "warm" and actual is not None) or (
            mode != "warm" and (actual is None or receipt["callbacks"] != [actual])
        ):
            raise ValueError("contract final callback audit disagrees with case")
        receipts.append(item)
    if observed != expected.keys():
        raise ValueError("contract case matrix incomplete")
    startup = index["startupExecutionId"]
    if (
        not isinstance(startup, str)
        or not startup.startswith("probe-")
        or (set(capture_records) != {startup})
    ):
        raise ValueError("contract startup audit or unexpected callback")
    verify_contract_cleanup(run_dir, executor)
    inventory = json.loads((run_dir / "owned-resources.json").read_bytes())
    qualification = {
        "schemaVersion": 1,
        "source": matrix["source"],
        "corpusHashes": matrix["corpusHashes"],
        "subset": matrix["subset"],
        "images": matrix["images"],
        "exclusions": EXCLUSIONS,
        "counts": counts,
        "receipts": receipts,
        "cleanupVerified": True,
        "finalAudit": audit_entry,
        "ownedResources": inventory,
    }
    # Hard-link a complete same-filesystem file: atomic publication, no replacement.
    with tempfile.NamedTemporaryFile(dir=run_dir, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(qualification, indent=2, sort_keys=True).encode())
    try:
        os.link(temporary, marker)
    finally:
        temporary.unlink()
    return marker
