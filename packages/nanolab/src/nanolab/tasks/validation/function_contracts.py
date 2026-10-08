"""Independent body/status/callback semantics for packaged function artifacts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import struct
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.assets.diagnostics.native_k8s_runtime import NATIVE_ERRORS
from nanolab.config.contract import ContractConfig
from nanolab.functions.contracts import (
    ContractCase,
    JsonValue,
    resolve_contract_matrix,
    same_json,
    strict_json,
)
from nanolab.tasks.validation.contract_resources import (
    ContractExecutor,
    ContractImage,
    ContractRuntime,
    artifact_container_resource,
    contract_command,
    inspect_contract_object,
    validate_native_binary,
    write_receipt,
)
from nanolab.workspace.recipe import RecipeRun


def check_contract_logs(raw: bytes, *, limit: int) -> None:
    """Reject bounded runtime diagnostics without rejecting expected business JSON."""
    if len(raw) >= limit:
        raise ValueError("contract log byte bound reached")
    markers = (
        *NATIVE_ERRORS,
        "No serializer found",
        "SerializationException",
        "Traceback (most recent call last)",
        "panicked at",
    )
    text = raw.decode(errors="replace")
    if any(marker in text for marker in markers):
        raise ValueError("contract runtime diagnostic")


def validate_png(raw: bytes, *, size: int) -> None:
    """Validate PNG structure and bounded scanlines, including Adam7 passes."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    offset, compressed, palette, ended = 8, bytearray(), False, False
    data_started, data_ended = False, False
    ihdr = None
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError("truncated PNG chunk")
        length = int.from_bytes(raw[offset : offset + 4], "big")
        kind = raw[offset + 4 : offset + 8]
        if any(not (65 <= char <= 90 or 97 <= char <= 122) for char in kind) or (
            kind[2] & 32
        ):
            raise ValueError("invalid PNG chunk name")
        if data_started and kind != b"IDAT":
            data_ended = True
        finish = offset + 12 + length
        if finish > len(raw):
            raise ValueError("truncated PNG data")
        body = raw[offset + 8 : finish - 4]
        crc = int.from_bytes(raw[finish - 4 : finish], "big")
        if zlib.crc32(kind + body) != crc:
            raise ValueError("invalid PNG CRC")
        if ihdr is None and kind != b"IHDR":
            raise ValueError("PNG IHDR must be first")
        if kind == b"IHDR":
            if ihdr is not None or length != 13:
                raise ValueError("invalid PNG IHDR")
            ihdr = struct.unpack("!IIBBBBB", body)
        elif kind == b"PLTE":
            if palette or compressed or not length or length % 3 or length > 768:
                raise ValueError("invalid PNG palette")
            palette = True
        elif kind == b"IDAT":
            if data_ended:
                raise ValueError("PNG data chunks must be consecutive")
            data_started = True
            compressed.extend(body)
        elif kind == b"IEND":
            if length or finish != len(raw):
                raise ValueError("invalid PNG ending")
            ended = True
        elif not kind[0] & 32:
            raise ValueError("unsupported PNG critical chunk")
        offset = finish
    if ihdr is None or not ended or not compressed:
        raise ValueError("incomplete PNG")
    width, height, depth, color, compression, filtering, interlace = ihdr
    depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if (
        width != size
        or height != size
        or depth not in depths.get(color, ())
        or compression
        or filtering
        or interlace not in (0, 1)
        or (color == 3 and not palette)
    ):
        raise ValueError("invalid PNG dimensions or format")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = (
        ((0, 0, 1, 1),)
        if interlace == 0
        else (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
    )
    rows = []
    for x, y, dx, dy in passes:
        columns, count = (
            max(0, (width - x + dx - 1) // dx),
            max(0, (height - y + dy - 1) // dy),
        )
        if columns and count:
            rows.extend([1 + (columns * channels * depth + 7) // 8] * count)
    expected = sum(rows)
    try:
        decoder = zlib.decompressobj()
        scanlines = decoder.decompress(compressed, expected + 1)
    except zlib.error as error:
        raise ValueError("invalid PNG compression") from error
    if (
        len(scanlines) != expected
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("invalid PNG scanline length")
    offset = 0
    for length in rows:
        if scanlines[offset] > 4:
            raise ValueError("invalid PNG scanline filter")
        offset += length


def _headers(value: JsonValue) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(item, str) for item in value.values()
    ):
        raise ValueError("invalid response headers")
    return {key.lower(): str(item) for key, item in value.items()}


def _raw(value: JsonValue) -> bytes:
    if not isinstance(value, str):
        raise ValueError("missing raw response body")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid base64 body") from error


def _png_output(
    output: JsonValue, headers: JsonValue, encoding: JsonValue, size: int
) -> bytes:
    if (
        _headers(headers).get("content-type", "").split(";", 1)[0].strip().lower()
        != "image/png"
        or encoding != "base64"
    ):
        raise ValueError("PNG content type or base64 encoding contract missing")
    raw = _raw(output)
    validate_png(raw, size=size)
    return raw


def validate_case_observation(
    case: ContractCase,
    *,
    mode: Literal["sdk", "warm", "one-shot"],
    http: dict[str, JsonValue] | None,
    callbacks: tuple[dict[str, JsonValue], ...],
    exit_code: int | None,
) -> None:
    """Compare independently specified output; transport success is insufficient."""
    if mode not in {"sdk", "warm", "one-shot"}:
        raise ValueError("unknown artifact mode")
    http_png = None
    if mode == "one-shot":
        if http is not None or type(exit_code) is not int or exit_code != 0:
            raise ValueError("one-shot exit must be zero without an HTTP response")
    else:
        if (
            http is None
            or type(http.get("status")) is not int
            or http["status"] != case.status
        ):
            raise ValueError("HTTP status disagrees with oracle")
        body = strict_json(_raw(http.get("bodyBase64")))
        if case.png_size is not None:
            headers = _headers(http.get("headers"))
            if headers.get("x-nanofaas-function-status") != "true":
                raise ValueError("PNG envelope marker missing")
            http_png = _png_output(
                body,
                http.get("headers"),
                headers.get("x-nanofaas-encoding"),
                case.png_size,
            )
        elif not same_json(body, case.expected):
            raise ValueError("HTTP body disagrees with oracle")
    if mode == "warm":
        if callbacks:
            raise ValueError("warm watchdog must have zero callbacks")
        return
    if len(callbacks) != 1:
        raise ValueError("exactly one callback required")
    callback = callbacks[0]
    execution_id = callback.get("executionId")
    if (
        not isinstance(execution_id, str)
        or callback.get("method") != "POST"
        or callback.get("path") != f"/v1/executions/{execution_id}:complete"
        or (http is not None and execution_id != http.get("executionId"))
    ):
        raise ValueError("callback execution identity disagrees")
    payload = strict_json(_raw(callback.get("bodyBase64")))
    if (
        not isinstance(payload, dict)
        or payload.get("success") is not True
        or payload.get("error") is not None
        or "output" not in payload
    ):
        raise ValueError("callback infrastructure error or malformed result")
    status = payload.get("statusCode", 200)
    if type(status) is not int or status != case.status:
        raise ValueError("callback status disagrees with oracle")
    if case.png_size is not None:
        raw = _png_output(
            payload["output"],
            payload.get("headers"),
            payload.get("encoding"),
            case.png_size,
        )
        if http_png is not None and raw != http_png:
            raise ValueError("HTTP and callback PNG bytes differ")
    elif not same_json(payload["output"], case.expected):
        raise ValueError("callback body disagrees with oracle")


@dataclass(frozen=True, slots=True)
class ContractEvidence:
    """Complete required counts and immutable case receipts, before outer cleanup."""

    case_paths: tuple[Path, ...]
    expected_http: int
    expected_one_shot: int
    expected_callbacks: int


class FunctionContractsTask(Task[ContractEvidence]):
    """Exercise packaged runtimes directly and audit callbacks after shutdown."""

    def __init__(
        self,
        source: Resource[RecipeRun],
        images: Resource[tuple[ContractImage, ...]],
        runtime: Resource[ContractRuntime],
        *,
        selectors: tuple[str, ...],
        settings: ContractConfig,
        executor: CommandTaskExecutor,
        run_dir: Path,
    ) -> None:
        """Bind independent frozen oracles to the owned artifact/capture resources."""
        self.source, self.images, self.runtime = source, images, runtime
        self.selectors, self.settings, self.run_dir = selectors, settings, run_dir
        self.executor = ContractExecutor(executor, run_dir, settings)
        self.title = "Qualify packaged functions and watchdog"
        self.case_paths: list[Path] = []
        self.expected_ids: dict[str, int] = {}
        self.capture_id = ""

    def _probe(
        self,
        inputs: TaskInputs,
        url: str,
        *,
        method: str = "GET",
        body: JsonValue = None,
        headers: dict[str, str] | None = None,
        budget: float | None = None,
    ) -> dict[str, Any]:
        settings = self.settings.model_dump()
        if budget is not None:
            settings["request_seconds"] = min(
                settings["request_seconds"], max(0.001, budget)
            )
        instruction = {
            "url": url,
            "method": method,
            "settings": settings,
            "headers": headers or {},
            "bodyBase64": base64.b64encode(json.dumps(body).encode()).decode()
            if method != "GET"
            else "",
        }
        result = contract_command(
            self.executor,
            inputs,
            (
                "docker",
                "exec",
                self.capture_id,
                "python3",
                "/app/artifact_contract.py",
                "probe",
                json.dumps(instruction),
            ),
            deadline=settings["request_seconds"] + 2,
        )
        value = strict_json(result.stdout)
        if not isinstance(value, dict) or type(value.get("status")) is not int:
            raise ValueError("invalid contract probe observation")
        return cast(dict[str, Any], value)

    def _register(self, inputs: TaskInputs, execution_id: str, expected: int) -> None:
        response = self._probe(
            inputs,
            "http://capture:8081/_nanolab/register",
            method="POST",
            body={"executionId": execution_id, "expectedCallbacks": expected},
        )
        if response["status"] != 200:
            raise ValueError("callback ID registration failed")
        self.expected_ids[execution_id] = expected

    def _snapshot(self, inputs: TaskInputs, *, final: bool = False) -> dict[str, Any]:
        response = self._probe(inputs, "http://capture:8081/_nanolab/records")
        if response["status"] != 200:
            raise ValueError("callback inspection failed")
        snapshot = strict_json(_raw(response["bodyBase64"]))
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("records"), list)
            or not isinstance(snapshot.get("violations"), list)
        ):
            raise ValueError("malformed callback inspection")
        value = cast(dict[str, Any], snapshot)
        for record in value["records"]:
            sequence = record["sequence"]
            path = self.run_dir / "capture" / f"{sequence:04d}.raw"
            if not path.exists():
                raw_response = self._probe(
                    inputs, f"http://capture:8081/_nanolab/body/{sequence}"
                )
                if raw_response["status"] != 200:
                    raise ValueError("callback raw evidence missing")
                raw = _raw(raw_response["bodyBase64"])
                if (
                    len(raw) >= self.settings.message_bytes
                    or hashlib.sha256(raw).hexdigest() != record["bodySha256"]
                ):
                    raise ValueError("callback raw identity or bound disagrees")
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(raw)
                write_receipt(path.with_suffix(".json"), record)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != record["bodySha256"]:
                raise ValueError("callback retained evidence changed")
            record["bodyBase64"] = base64.b64encode(raw).decode()
        write_receipt(self.run_dir / "capture" / f"snapshot-{uuid4().hex}.json", value)
        if value["violations"]:
            raise ValueError("callback capture violation: " + str(value["violations"]))
        if final and (
            type(value.get("activeCallbacks")) is not int
            or value["activeCallbacks"] != 0
        ):
            raise ValueError("callback still pending during final audit")
        return value

    def _ready(self, inputs: TaskInputs, url: str) -> None:
        deadline = time.monotonic() + self.settings.readiness_seconds
        while time.monotonic() < deadline:
            try:
                if (
                    self._probe(inputs, url, budget=deadline - time.monotonic())[
                        "status"
                    ]
                    == 200
                ):
                    return
            except RuntimeError:
                pass
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise ValueError("artifact readiness deadline reached")

    def _callbacks(
        self, inputs: TaskInputs, execution_id: str, expected: int
    ) -> tuple[dict[str, JsonValue], ...]:
        deadline = time.monotonic() + self.settings.callback_seconds
        while True:
            snapshot = self._snapshot(inputs)
            callbacks = tuple(
                record
                for record in snapshot["records"]
                if record["executionId"] == execution_id
            )
            if len(callbacks) >= expected:
                return callbacks
            if time.monotonic() >= deadline:
                raise ValueError("callback arrival deadline reached")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _identity(
        self, inputs: TaskInputs, image: ContractImage, container_id: str
    ) -> None:
        current = inspect_contract_object(
            self.executor, inputs, "image", image.cell.image
        )
        container = inspect_contract_object(
            self.executor, inputs, "container", container_id
        )
        if (
            current is None
            or container is None
            or current.get("Id") != image.image_id
            or current.get("Architecture") != image.architecture
            or tuple(current.get("Config", {}).get("Entrypoint") or ())
            != image.entrypoint
            or container.get("Image") != image.image_id
            or tuple(container.get("Config", {}).get("Entrypoint") or ())
            != image.entrypoint
        ):
            raise ValueError("artifact image/container identity changed")
        if image.cell.flavor == "native":
            binary = self.run_dir / "native-live" / f"{uuid4().hex}.elf"
            binary.parent.mkdir(parents=True, exist_ok=True)
            contract_command(
                self.executor,
                inputs,
                ("docker", "cp", f"{container_id}:{image.entrypoint[0]}", str(binary)),
            )
            if (
                validate_native_binary(binary.read_bytes(), image.architecture)
                != image.native_sha256
            ):
                raise ValueError("running native executable identity changed")

    def _case(
        self,
        inputs: TaskInputs,
        image: ContractImage,
        case: ContractCase,
        *,
        mode: Literal["sdk", "warm", "one-shot"],
        index: int,
        container_id: str,
        execution_id: str,
    ) -> None:
        identifier = f"{image.cell.target.name}-{image.cell.flavor}/{mode}/{index:03d}"
        path = self.run_dir / "cases" / identifier / "receipt.json"
        http, callbacks, exit_code, failure = None, (), None, None
        try:
            if mode == "one-shot":
                result = contract_command(
                    self.executor,
                    inputs,
                    ("docker", "wait", container_id),
                    deadline=self.settings.request_seconds,
                )
                exit_code = int(result.stdout.strip())
            else:
                http = self._probe(
                    inputs,
                    "http://artifact:8080/invoke",
                    method="POST",
                    body={"input": case.input},
                    headers={
                        "Content-Type": "application/json",
                        "X-Execution-Id": execution_id,
                    },
                )
                http["executionId"] = execution_id
            callbacks = self._callbacks(
                inputs, execution_id, 0 if mode == "warm" else 1
            )
            self._identity(inputs, image, container_id)
            validate_case_observation(
                case, mode=mode, http=http, callbacks=callbacks, exit_code=exit_code
            )
        except BaseException as error:
            failure = f"{type(error).__name__}: {error}"
            raise
        finally:
            write_receipt(
                path,
                {
                    "id": identifier,
                    "executionId": execution_id,
                    "imageId": image.image_id,
                    "containerId": container_id,
                    "case": asdict(case),
                    "http": http,
                    "callbacks": callbacks,
                    "exitCode": exit_code,
                    "status": "failed" if failure else "passed",
                    "error": failure,
                },
            )
            if http is not None:
                with (path.parent / "response.raw").open("xb") as stream:
                    stream.write(_raw(http["bodyBase64"]))
            self.case_paths.append(path)

    def _artifact(
        self,
        inputs: TaskInputs,
        image: ContractImage,
        runtime: ContractRuntime,
        cases: tuple[ContractCase, ...],
        *,
        mode: Literal["sdk", "warm", "one-shot"],
        index: int | None = None,
    ) -> None:
        execution_id = uuid4().hex if mode == "one-shot" else None
        if execution_id is not None:
            self._register(inputs, execution_id, 1)
        selected = (
            ((index, cases[index]),) if index is not None else tuple(enumerate(cases))
        )
        resource = artifact_container_resource(
            image,
            runtime,
            mode=mode,
            execution_id=execution_id,
            payload={"input": selected[0][1].input} if mode == "one-shot" else None,
            executor=self.executor,
            run_dir=self.run_dir,
        )
        container_id = resource.acquire(inputs)
        try:
            if mode != "one-shot":
                self._ready(inputs, "http://artifact:8080/health")
            for case_index, case in selected:
                current_id = execution_id or uuid4().hex
                if execution_id is None:
                    self._register(inputs, current_id, 0 if mode == "warm" else 1)
                self._case(
                    inputs,
                    image,
                    case,
                    mode=mode,
                    index=case_index,
                    container_id=container_id,
                    execution_id=current_id,
                )
        finally:
            try:
                if mode != "one-shot":
                    contract_command(
                        self.executor,
                        inputs,
                        ("docker", "stop", "--time", "5", container_id),
                    )
                logs = contract_command(
                    self.executor, inputs, ("docker", "logs", container_id)
                ).stdout.encode()
                log_path = self.run_dir / "container-logs" / f"{container_id}.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("xb") as stream:
                    stream.write(logs)
                check_contract_logs(logs, limit=self.settings.log_bytes)
            finally:
                resource.release(inputs, container_id)
        time.sleep(self.settings.quiet_seconds)
        self._snapshot(inputs, final=True)

    def run(self, inputs: TaskInputs) -> TaskOutcome[ContractEvidence]:
        """Retain failed receipts and never write the post-cleanup success marker."""
        runtime, images = inputs.resource(self.runtime), inputs.resource(self.images)
        self.capture_id = runtime.capture_container
        source = inputs.resource(self.source).source_dir
        matrix = resolve_contract_matrix(
            source,
            self.selectors,
            architecture=images[0].cell.architecture,
            tag=runtime.owner,
        )
        if tuple(image.cell for image in images) != matrix.images.cells:
            raise ValueError("built images do not cover the frozen matrix")
        expected_http = sum(
            len(matrix.cases[family])
            for image in images
            for family in matrix.cases
            if image.cell.target.name.endswith("-" + family)
        )
        expected_one_shot = sum(
            len(matrix.cases[family])
            for image in images
            for family in matrix.cases
            if image.cell.target.name == "bash-" + family
        )
        expected_callbacks = expected_http
        status, audited = "failed", False
        try:
            self._ready(inputs, "http://capture:8081/health")
            probe_id = "probe-" + uuid4().hex
            self._register(inputs, probe_id, 1)
            probe = self._probe(
                inputs,
                f"http://capture:8081/v1/executions/{probe_id}:complete",
                method="POST",
                body={"success": True, "output": {"probe": True}},
            )
            if probe["status"] != 200 or len(self._callbacks(inputs, probe_id, 1)) != 1:
                raise ValueError("capture startup self-check failed")
            for image in images:
                family = next(
                    family
                    for family in matrix.cases
                    if image.cell.target.name.endswith("-" + family)
                )
                cases = matrix.cases[family]
                warm = image.cell.target.name.startswith("bash-")
                self._artifact(
                    inputs, image, runtime, cases, mode="warm" if warm else "sdk"
                )
                if warm:
                    for index in range(len(cases)):
                        self._artifact(
                            inputs, image, runtime, cases, mode="one-shot", index=index
                        )
            snapshot = self._snapshot(inputs, final=True)
            for execution_id, expected in self.expected_ids.items():
                if (
                    sum(
                        record["executionId"] == execution_id
                        for record in snapshot["records"]
                    )
                    != expected
                ):
                    raise ValueError("final callback count disagrees")
            logs = contract_command(
                self.executor, inputs, ("docker", "logs", self.capture_id)
            ).stdout.encode()
            with (self.run_dir / "capture.log").open("xb") as stream:
                stream.write(logs)
            check_contract_logs(logs, limit=self.settings.log_bytes)
            audited, status = True, "passed"
            return TaskOutcome(
                value=ContractEvidence(
                    tuple(self.case_paths),
                    expected_http,
                    expected_one_shot,
                    expected_callbacks,
                )
            )
        finally:
            write_receipt(
                self.run_dir / "case-index.json",
                {
                    "status": status,
                    "audited": audited,
                    "expectedHttp": expected_http,
                    "expectedOneShot": expected_one_shot,
                    "expectedCallbacks": expected_callbacks,
                    "cases": [
                        {
                            "path": str(path.relative_to(self.run_dir)),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                        for path in self.case_paths
                    ],
                },
            )
