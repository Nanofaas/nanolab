"""Measured topology facts and explicit checks of experiment assumptions."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask

from nanolab.config.one_shot import operational_limit
from nanolab.one_shot.infrastructure import NodeExecutor, NodeResource, OneShotResources
from nanolab.one_shot.topology import observe_neighbors
from nanolab.tasks.managed_containers import inspect_managed_container


class NodeEvidence(BaseModel):
    """Measured VM, network and runtime facts; missing facts have no defaults."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    node_id: str
    kind: Literal["edge", "cloud"]
    vm_name: str
    host: str
    cpus: int = Field(gt=0)
    memory_mib: int = Field(gt=0)
    memory_capacity_mib: int = Field(gt=0)
    backend: Literal["container-local"]
    concurrency: int
    hpa_active: bool
    ready: bool
    peer_count: int = Field(ge=0)
    neighbor_ids: list[str] | None = None
    image_digests: dict[str, str]
    clock_offset_seconds: float | None
    clock_uncertainty_seconds: float | None = Field(ge=0)
    rtt_seconds: float | None = Field(ge=0)
    clock_status: Literal["MEASURED", "MISSING"] = "MEASURED"
    bandwidth_mbps: float = Field(gt=0)
    os: str | None = None
    architecture: str | None = None
    runtime_limits: dict[str, dict] | None = None
    host_cpu_binding: dict | None = None


class TopologyEvidence(BaseModel):
    """Topology with explicit shared-host contention and measurement time."""

    schema_version: Literal[1] = 1
    provider: Literal["multipass"] = "multipass"
    purpose: Literal["workflow-validation", "scientific-experiment"] = (
        "workflow-validation"
    )
    measured_at: datetime
    generator_location: str
    shared_physical_host: bool = True
    nodes: list[NodeEvidence]
    logging: dict | None = None
    peer_graph: dict | None = None
    host_cpu_placement: dict | None = None


def validate_node(
    node: NodeEvidence,
    *,
    max_clock_skew_seconds: float,
    protocol_version: Literal[1, 2] = 1,
) -> None:
    """Refuse unsupported physical or readiness assumptions before load."""
    if not node.ready:
        raise ValueError(f"{node.node_id} is not ready")
    if node.hpa_active:
        raise ValueError("HPA must be disabled")
    if node.memory_capacity_mib >= node.memory_mib:
        raise ValueError("replica memory exceeds VM memory budget")
    if node.concurrency != 1:
        raise ValueError("physical concurrency must equal one")
    if protocol_version == 1 and (
        node.clock_status != "MEASURED"
        or node.clock_offset_seconds is None
        or node.clock_uncertainty_seconds is None
        or abs(node.clock_offset_seconds) + node.clock_uncertainty_seconds
        > max_clock_skew_seconds
    ):
        raise ValueError("clock offset/uncertainty missing or exceeds threshold")
    if node.kind == "edge" and node.peer_count < 1:
        raise ValueError("edge has no discovered peer")
    if not node.image_digests or any(
        not digest.startswith("sha256:") or len(digest) != 71
        for digest in node.image_digests.values()
    ):
        raise ValueError("image digest missing or invalid")


def measure_clock(
    node: NodeResource, *, repetitions: int = 3
) -> tuple[float, float, datetime]:
    """Estimate offset using real remote timestamps and bounded RTT uncertainty."""
    samples = []
    for _ in range(repetitions):
        before = time.time()
        remote = float(
            node.command(
                ("python3", "-c", "import time; print(time.time())")
            ).stdout.strip()
        )
        after = time.time()
        samples.append((after - before, remote - (before + after) / 2, remote))
    rtt, offset, measured = min(samples)
    return offset, rtt / 2, datetime.fromtimestamp(measured, UTC)


class PreflightTask(Task[TopologyEvidence]):
    """Validate runtime facts collected by the deployment task under VM ownership."""

    title = "Validate one-shot topology and physical assumptions"

    def __init__(
        self,
        resources: OneShotResources,
        *,
        evidence: list[NodeEvidence],
        max_clock_skew_seconds: float,
    ) -> None:
        """Bind declared resources and evidence without provisioning."""
        self.resources, self.evidence = resources, evidence
        self.max_clock_skew_seconds = max_clock_skew_seconds

    def run(self, inputs: TaskInputs) -> TaskOutcome[TopologyEvidence]:
        """Check complete node evidence while resources are acquired."""
        for node in self.resources.nodes.values():
            inputs.resource(node.vm)
        if {node.node_id for node in self.evidence} != set(self.resources.nodes):
            raise ValueError("incomplete topology evidence")
        for node in self.evidence:
            validate_node(node, max_clock_skew_seconds=self.max_clock_skew_seconds)
        return TaskOutcome(
            value=TopologyEvidence(
                measured_at=datetime.now(UTC),
                generator_location=self.resources.generator_location,
                nodes=self.evidence,
            )
        )


def measure_http_clock(
    url: str,
    *,
    http: httpx.Client,
    repetitions: int = 10,
    record_probe: Callable[[dict], None] | None = None,
    tolerate_failures: bool = False,
    timeout_seconds: float = 1,
) -> tuple[float, float, datetime]:
    """Keep UTC diagnostics and monotonic brackets, including failed attempts."""
    samples = []
    last_failure: Exception | None = None
    for attempt in range(repetitions):
        before = time.time()
        before_mono = time.monotonic()
        remote = None
        remote_mono = None
        remote_boot = None
        failure = None
        try:
            response = http.get(url + "/clock", timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            remote = float(payload["timestamp"])
            if not math.isfinite(remote):
                raise ValueError("non-finite remote clock")
            if "monotonicSeconds" in payload:
                remote_mono = float(payload["monotonicSeconds"])
                remote_boot = payload["bootId"]
                if (
                    not math.isfinite(remote_mono)
                    or remote_mono < 0
                    or not isinstance(remote_boot, str)
                    or not remote_boot
                ):
                    raise ValueError("invalid remote monotonic clock identity")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            failure = error
            last_failure = error
        after_mono = time.monotonic()
        after = time.time()
        rtt = after_mono - before_mono
        jump = after - before - rtt
        if record_probe is not None:
            record_probe(
                {
                    "attempt": attempt,
                    "beforeUtcSeconds": before,
                    "afterUtcSeconds": after,
                    "beforeMonotonicSeconds": before_mono,
                    "afterMonotonicSeconds": after_mono,
                    "remoteUtcSeconds": remote,
                    "remoteMonotonicSeconds": remote_mono,
                    "remoteBootId": remote_boot,
                    "rttSeconds": rtt,
                    "hostUtcJumpSeconds": jump,
                    "errorType": type(failure).__name__ if failure else None,
                }
            )
        if failure is not None:
            if not tolerate_failures:
                raise failure
            continue
        if remote is None:
            raise RuntimeError("clock probe completed without timestamps")
        # A host UTC step widens UTC uncertainty, without corrupting the RTT
        # or host/guest monotonic bracket used for experiment correlation.
        uncertainty = (rtt + abs(jump)) / 2
        samples.append((uncertainty, remote - (before + after) / 2, remote))
    if not samples:
        if last_failure is not None:
            raise last_failure
        raise ValueError("clock probe requires at least one successful attempt")
    uncertainty, offset, measured = min(samples)
    return offset, uncertainty, datetime.fromtimestamp(measured, UTC)


def runtime_endpoint(
    node: NodeResource, function: str, *, inputs: TaskInputs, replica: int = 1
) -> tuple[str, dict]:
    """Resolve replica one by verified managed-container labels."""
    observed = inspect_managed_container(
        function=f"{node.request.name}/{function}",
        replica=replica,
        executor=NodeExecutor(node),
        role="host",
        inputs=inputs,
    )
    ports = observed["NetworkSettings"]["Ports"]["8080/tcp"]
    if not isinstance(ports, list) or not ports:
        raise ValueError("runtime published port missing")
    host = inputs.resource(node.vm).host
    return f"http://{host}:{int(ports[0]['HostPort'])}", observed


def runtime_inventory(
    node: NodeResource, *, functions: list[str], inputs: TaskInputs
) -> list[tuple[str, int, str, dict]]:
    """Resolve current owned replicas in two commands instead of querying all maxima."""
    executor = NodeExecutor(node)
    result = (
        CommandTask(
            title="List current managed replicas",
            argv=(
                "docker",
                "ps",
                "--no-trunc",
                "-q",
                "--filter",
                "label=io.nanofaas.managed=true",
            ),
            executor=executor,
            role="host",
        )
        .run(inputs)
        .value
    )
    identifiers = result.stdout.split() if result else []
    if not identifiers:
        return []
    if any(
        re.fullmatch(r"[0-9a-f]{64}", identifier) is None for identifier in identifiers
    ) or len(set(identifiers)) != len(identifiers):
        raise ValueError("runtime inventory identity mismatch")
    result = (
        CommandTask(
            title="Inspect current managed replicas",
            argv=("docker", "inspect", "--format={{json .}}", *identifiers),
            executor=executor,
            role="host",
        )
        .run(inputs)
        .value
    )
    objects = (
        [json.loads(line) for line in result.stdout.splitlines()] if result else []
    )
    if {obj.get("Id") for obj in objects} != set(identifiers) or len(objects) != len(
        identifiers
    ):
        raise ValueError("runtime inventory identity mismatch")
    if node.request.name is None:
        raise ValueError("runtime inventory requires owned VM identity")
    prefix = node.request.name + "/"
    inventory = []
    seen = set()
    for obj in objects:
        labels = obj.get("Config", {}).get("Labels", {})
        function = labels.get("io.nanofaas.function", "")
        if not function.startswith(prefix) or function[len(prefix) :] not in functions:
            continue
        name = function[len(prefix) :]
        replica = labels.get("io.nanofaas.replica", "")
        if not isinstance(replica, str) or not replica.isdecimal() or int(replica) < 1:
            raise ValueError("runtime inventory invalid replica")
        key = (name, int(replica))
        if (
            key in seen
            or labels.get("io.nanofaas.managed") != "true"
            or obj.get("State", {}).get("Running") is not True
        ):
            raise ValueError("runtime inventory replica identity mismatch")
        seen.add(key)
        ports = obj["NetworkSettings"]["Ports"]["8080/tcp"]
        if (
            not isinstance(ports, list)
            or not ports
            or len({port["HostPort"] for port in ports}) != 1
            or not 0 < int(ports[0]["HostPort"]) < 65536
        ):
            raise ValueError("runtime inventory published port missing")
        inventory.append(
            (
                name,
                int(replica),
                f"http://{inputs.resource(node.vm).host}:{int(ports[0]['HostPort'])}",
                obj,
            )
        )
    return inventory


class CollectTopologyTask(Task[TopologyEvidence]):
    """Collect physical facts through SDK APIs and verified Docker metadata."""

    title = "Measure and validate deployed one-shot topology"

    def __init__(
        self,
        resources: OneShotResources,
        *,
        endpoints,
        probes,
        distribution,
        function_settings,
        output,
        max_clock_skew_seconds: float = 0.1,
        collectors=None,
        neighbor_graph=None,
        node_function_settings=None,
        purpose: Literal[
            "workflow-validation", "scientific-experiment"
        ] = "workflow-validation",
        strict_memory: bool = False,
        require_no_swap: bool = True,
        protocol_version: Literal[1, 2] = 1,
        settings=None,
    ):
        """Bind explicit resource handles and expected physical identities."""
        self.settings = settings
        self.collectors = collectors or {}
        self.neighbor_graph = neighbor_graph or {
            key: tuple(
                other.config.id
                for other in resources.nodes.values()
                if other.config.kind == "edge" and other.config.id != key
            )
            for key, node in resources.nodes.items()
            if node.config.kind == "edge"
        }
        self.node_function_settings = node_function_settings or {}
        self.purpose: Literal["workflow-validation", "scientific-experiment"] = purpose
        self.strict_memory = strict_memory
        self.require_no_swap = require_no_swap
        self.protocol_version: Literal[1, 2] = protocol_version
        self.resources, self.endpoints, self.probes = resources, endpoints, probes
        self.distribution, self.function_settings = distribution, function_settings
        self.output, self.max_clock_skew_seconds = output, max_clock_skew_seconds

    def run(self, inputs: TaskInputs) -> TaskOutcome[TopologyEvidence]:
        """Preserve partial observations even when collection or validation fails."""
        self.observations = {}
        self.output.write_text(
            json.dumps(
                {"status": "COLLECTING", "provider": "multipass", "observations": {}}
            )
            + "\n"
        )
        try:
            return self._collect(inputs)
        except BaseException as error:
            self.output.write_text(
                json.dumps(
                    {
                        "status": "FAIL",
                        "provider": "multipass",
                        "purpose": self.purpose,
                        "observations": self.observations,
                        "reason": str(error),
                    },
                    indent=2,
                )
                + "\n"
            )
            raise

    def _collect(self, inputs: TaskInputs) -> TaskOutcome[TopologyEvidence]:
        """Save observed assumptions and fail before any experimental load."""
        from nanolab.one_shot.client import NanoFaasOneShotClient

        evidence = []
        placement = self.resources.host_cpu_placement
        if placement is not None:
            from nanolab.one_shot.host_cpu import collect_host_placement

            self.observations["hostCpuPolicy"] = placement.model_dump(by_alias=True)
            self.observations["hostCpuPlacement"] = collect_host_placement(
                placement,
                {
                    node_id: inputs.resource(node.vm).name
                    for node_id, node in self.resources.nodes.items()
                },
            )
        built = inputs.resource(self.distribution)
        expected_images = {
            component.name: component.image.id
            for component in built.components
            if component.kind == "function"
        }
        with httpx.Client(
            trust_env=False, timeout=operational_limit(self.settings, "api_seconds", 30)
        ) as http:
            for key, node in self.resources.nodes.items():
                vm = inputs.resource(node.vm)
                url = inputs.resource(self.endpoints[key])
                probe = inputs.resource(self.probes[key])
                inventory_response = http.get(probe + "/inventory")
                inventory_response.raise_for_status()
                inventory = inventory_response.json()
                self.observations[key] = {"inventory": inventory}
                peer_count = 0
                peers: list[str] = []
                probe_rtts: list[float] = []

                def record_probe(
                    sample: dict,
                    node_id: str = key,
                    rtts: list[float] = probe_rtts,
                ) -> None:
                    raw_output = self.output.with_name(
                        self.output.stem + "-probes.jsonl"
                    )
                    with raw_output.open("a") as raw:
                        raw.write(json.dumps({"node": node_id, **sample}) + "\n")
                    if (
                        sample.get("errorType") is None
                        and sample.get("remoteUtcSeconds") is not None
                    ):
                        rtts.append(sample["rttSeconds"])

                clock_status = "MEASURED"
                try:
                    clock, uncertainty, measured_at = measure_http_clock(
                        probe,
                        http=http,
                        record_probe=record_probe,
                        tolerate_failures=self.protocol_version == 2,
                        timeout_seconds=operational_limit(
                            self.settings, "collection_http_seconds", 1
                        ),
                    )
                    self.observations[key]["clock"] = {
                        "status": clock_status,
                        "offsetSeconds": clock,
                        "uncertaintySeconds": uncertainty,
                        "measuredAt": measured_at.isoformat(),
                    }
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                    if self.protocol_version == 1:
                        raise
                    clock_status = "MISSING"
                    clock, uncertainty, measured_at = None, None, None
                    self.observations[key]["clock"] = {
                        "status": clock_status,
                        "errorType": type(error).__name__,
                        "error": str(error),
                    }
                if node.config.kind == "edge":
                    client = NanoFaasOneShotClient(
                        url,
                        http=http,
                        timeout_seconds=operational_limit(
                            self.settings, "api_seconds", 30
                        ),
                    )
                    if self.protocol_version == 1:
                        if clock is None or measured_at is None:
                            raise ValueError("missing required v1 clock evidence")
                        client.update_clock_health(
                            offset_seconds=clock, measured_at=measured_at
                        )
                    expected_neighbors = (
                        set(self.neighbor_graph[key])
                        if self.neighbor_graph
                        else {
                            other.config.id
                            for other in self.resources.nodes.values()
                            if other.config.kind == "edge" and other.config.id != key
                        }
                    )
                    deadline = time.monotonic() + operational_limit(
                        self.settings, "replica_start_seconds", 30
                    )
                    status = client.status()
                    while time.monotonic() < deadline:
                        status = client.status()
                        peers = [peer.peer_id for peer in status.peer_endpoints]
                        peer_count = len(peers)
                        if (
                            len(peers) != len(set(peers))
                            or set(peers) - expected_neighbors
                        ):
                            raise ValueError(
                                "operative neighbor identities differ from frozen graph"
                            )
                        if set(peers) == expected_neighbors:
                            break
                        time.sleep(0.2)
                    if set(peers) != expected_neighbors:
                        raise ValueError("incomplete peer neighbor membership")
                    self.observations[key]["neighbors"] = [
                        peer.model_dump(by_alias=True) for peer in status.peer_endpoints
                    ]
                listing = http.get(url + "/v1/functions")
                listing.raise_for_status()
                self.observations[key]["functions"] = [
                    {k: v for k, v in row.items() if k != "env"}
                    for row in listing.json()
                ]
                self.observations[key]["containers"] = node.command(
                    ("docker", "ps", "-a", "--format={{json .}}")
                ).stdout
                image_digests = {}
                runtime_limits = {}
                for function, expected in self.node_function_settings.get(
                    key, self.function_settings
                ).items():
                    deadline = time.monotonic() + operational_limit(
                        self.settings, "replica_start_seconds", 30
                    )
                    while True:
                        try:
                            runtime, observed = runtime_endpoint(
                                node, function, inputs=inputs
                            )
                            response = http.get(
                                runtime + "/runtime/status",
                                timeout=operational_limit(
                                    self.settings, "collection_http_seconds", 2
                                ),
                            )
                            response.raise_for_status()
                            physical = response.json()
                            break
                        except (httpx.HTTPError, RuntimeError):
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.2)
                    if (
                        physical.get("schemaVersion") != 1
                        or physical.get("physicalReleaseProof") is not True
                        or physical.get("maxConcurrentHandlers") != 1
                    ):
                        raise ValueError("physical concurrency/release proof missing")
                    image = observed["Image"]
                    if image != expected_images[function]:
                        raise ValueError("runtime image digest mismatch")
                    limits = observed["HostConfig"]
                    if limits["Memory"] != expected.memory_mib * 1024 * 1024 or limits[
                        "NanoCpus"
                    ] != int(expected.cpu * 1e9):
                        raise ValueError("runtime CPU/memory resource mismatch")
                    if self.strict_memory:
                        if limits.get("MemorySwap") != limits["Memory"]:
                            raise ValueError(
                                "strict runtime memory/swap resource mismatch"
                            )
                        sample = collect_container_cgroup(node, observed)
                        runtime_limits[function] = sample
                        self.observations[key]["runtimeLimits"] = runtime_limits
                        validate_strict_cgroup(
                            sample,
                            memory_mib=expected.memory_mib,
                            cpu=expected.cpu,
                            require_no_swap=self.require_no_swap,
                        )
                    spec_response = http.get(url + "/v1/functions/" + function)
                    spec_response.raise_for_status()
                    spec = spec_response.json()
                    if spec.get("runtimeMode") != "HTTP":
                        raise ValueError("explicit HTTP runtime identity is required")
                    if (
                        spec["scalingConfig"]["strategy"] != "NONE"
                        or spec["scalingConfig"]["concurrencyControl"][
                            "targetInFlightPerPod"
                        ]
                        != 1
                    ):
                        raise ValueError(
                            "HPA/scaling or concurrency assumptions violated"
                        )
                    image_digests[function] = image
                # Measure cross-VM transfer, not the operator's download path.
                other_key = next(other for other in self.probes if other != key)
                target = inputs.resource(self.probes[other_key])
                code = (
                    "import json,time,urllib.request; t=time.monotonic(); "
                    f"data=urllib.request.urlopen({json.dumps(target + '/bytes')},"
                    "timeout=10).read(); "
                    "elapsed=time.monotonic()-t; "
                    "print(json.dumps({'bytes':len(data),'seconds':elapsed}))"
                )
                network = json.loads(node.command(("python3", "-c", code)).stdout)
                if network["bytes"] != 8 * 1024 * 1024:
                    raise ValueError("incomplete network transfer")
                fact = NodeEvidence(
                    node_id=key,
                    kind=node.config.kind,
                    vm_name=vm.name,
                    host=vm.host,
                    cpus=inventory["cpus"],
                    memory_mib=inventory["memoryMiB"],
                    memory_capacity_mib=node.config.memory_capacity_mib,
                    backend="container-local",
                    concurrency=1,
                    hpa_active=False,
                    ready=True,
                    peer_count=peer_count,
                    neighbor_ids=peers if node.config.kind == "edge" else [],
                    image_digests=image_digests,
                    clock_offset_seconds=clock,
                    clock_uncertainty_seconds=uncertainty,
                    rtt_seconds=min(probe_rtts) if probe_rtts else None,
                    clock_status=clock_status,
                    bandwidth_mbps=network["bytes"] * 8 / network["seconds"] / 1e6,
                    os=inventory["os"],
                    architecture=inventory["architecture"],
                    runtime_limits=runtime_limits or None,
                    host_cpu_binding=self.observations.get("hostCpuPlacement", {})
                    .get("nodes", {})
                    .get(key),
                )
                validate_node(
                    fact,
                    max_clock_skew_seconds=self.max_clock_skew_seconds,
                    protocol_version=self.protocol_version,
                )
                if fact.cpus < node.config.cpus:
                    raise ValueError("VM CPU allocation mismatch")
                evidence.append(fact)
            if self.purpose == "scientific-experiment":
                self.observations["peerGraph"] = observe_neighbors(
                    http,
                    {
                        key: inputs.resource(self.endpoints[key])
                        for key in self.neighbor_graph
                    },
                    self.neighbor_graph,
                )
        topology = TopologyEvidence(
            purpose=self.purpose,
            peer_graph=self.observations.get("peerGraph"),
            host_cpu_placement=self.observations.get("hostCpuPlacement"),
            measured_at=datetime.now(UTC),
            generator_location=self.resources.generator_location,
            nodes=evidence,
            logging={
                key: inputs.resource(resource).identity
                for key, resource in self.collectors.items()
            }
            or None,
        )
        self.output.write_text(topology.model_dump_json(indent=2) + "\n")
        return TaskOutcome(value=topology)


_CGROUP_READ_SCRIPT = """
import json, subprocess, sys
from pathlib import Path
identifier, expected_pid = sys.argv[1], int(sys.argv[2])
def inspect():
    row = json.loads(subprocess.check_output(['docker', 'inspect', identifier]))[0]
    if (row['Id'] != identifier or row['State']['Pid'] != expected_pid
        or not row['State']['Running']):
        raise RuntimeError('container identity changed during cgroup collection')
    return row
before = inspect()
proc = Path('/proc') / str(expected_pid)
# starttime distinguishes reuse of a numeric PID; comm can contain spaces.
start = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
lines = (proc / 'cgroup').read_text().splitlines()
paths = [line[3:] for line in lines if line.startswith('0::')]
if len(paths) != 1:
    raise RuntimeError('unified cgroup v2 not available')
root = Path('/sys/fs/cgroup').resolve()
folder = (root / paths[0].lstrip('/')).resolve()
if not folder.is_relative_to(root) or folder == root:
    raise RuntimeError('invalid container cgroup path')
keys = ('memory.max', 'memory.swap.max', 'memory.current',
        'memory.peak', 'memory.events', 'cpu.max')
value = {key: (folder / key).read_text().strip() for key in keys}
after = inspect()
end = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
if (before['Config']['Labels'] != after['Config']['Labels'] or start != end
    or (proc / 'cgroup').read_text().splitlines() != lines):
    raise RuntimeError('container identity changed during cgroup collection')
value.update(containerId=identifier, pid=expected_pid, cgroup=paths[0])
print(json.dumps(value))
"""


def collect_container_cgroup(node: NodeResource, observed: dict) -> dict:
    """Read on the VM host: distroless function images need no shell or tools."""
    identifier = observed.get("Id", "")
    pid = observed.get("State", {}).get("Pid")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", identifier)
        or type(pid) is not int
        or pid <= 0
    ):
        raise ValueError("invalid managed container identity for cgroup collection")
    result = node.command(("python3", "-c", _CGROUP_READ_SCRIPT, identifier, str(pid)))
    sample = json.loads(result.stdout)
    if (
        not isinstance(sample, dict)
        or sample.get("containerId") != identifier
        or sample.get("pid") != pid
    ):
        raise ValueError("cgroup response container identity mismatch")
    return sample


def validate_strict_cgroup(
    sample: dict, *, memory_mib: int, cpu: float, require_no_swap: bool = True
) -> None:
    """Verify limits; an explicit observation can retain the known swap defect."""
    try:
        memory = int(sample["memory.max"])
        swap = sample["memory.swap.max"]
        if swap != "max":
            swap = int(swap)
        current, peak = int(sample["memory.current"]), int(sample["memory.peak"])
        quota, period = map(int, sample["cpu.max"].split())
        events = dict(line.split() for line in sample["memory.events"].splitlines())
        oom, killed = int(events["oom"]), int(events["oom_kill"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("missing or invalid cgroup limit evidence") from error
    if memory != memory_mib * 1024 * 1024 or (require_no_swap and swap != 0):
        raise ValueError("cgroup memory or swap limit mismatch")
    if swap != "max" and swap < 0:
        raise ValueError("invalid cgroup swap evidence")
    if period <= 0 or quota <= 0 or abs(quota / period - cpu) > 1e-6:
        raise ValueError("cgroup CPU quota mismatch")
    if current < 0 or peak < current or oom != 0 or killed != 0:
        raise ValueError("cgroup memory evidence contains OOM or inconsistent usage")
