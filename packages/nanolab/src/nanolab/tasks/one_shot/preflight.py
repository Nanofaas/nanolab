"""Measured topology facts and explicit checks of experiment assumptions."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sonata_engine import Task, TaskInputs, TaskOutcome

from nanolab.one_shot.infrastructure import NodeResource, OneShotResources


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
    image_digests: dict[str, str]
    clock_offset_seconds: float
    clock_uncertainty_seconds: float = Field(ge=0)
    rtt_seconds: float = Field(ge=0)
    bandwidth_mbps: float = Field(gt=0)
    os: str | None = None
    architecture: str | None = None


class TopologyEvidence(BaseModel):
    """Topology with explicit shared-host contention and measurement time."""

    schema_version: Literal[1] = 1
    provider: Literal["multipass"] = "multipass"
    purpose: Literal["workflow-validation"] = "workflow-validation"
    measured_at: datetime
    generator_location: str
    shared_physical_host: bool = True
    nodes: list[NodeEvidence]


def validate_node(node: NodeEvidence, *, max_clock_skew_seconds: float) -> None:
    """Refuse unsupported physical or readiness assumptions before load."""
    if not node.ready:
        raise ValueError(f"{node.node_id} is not ready")
    if node.hpa_active:
        raise ValueError("HPA must be disabled")
    if node.memory_capacity_mib >= node.memory_mib:
        raise ValueError("replica memory exceeds VM memory budget")
    if node.concurrency != 1:
        raise ValueError("physical concurrency must equal one")
    if (
        abs(node.clock_offset_seconds) + node.clock_uncertainty_seconds
        > max_clock_skew_seconds
    ):
        raise ValueError("clock offset/uncertainty exceeds threshold")
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
        samples.append((after - before, remote - (before + after) / 2))
    rtt, offset = min(samples)
    return offset, rtt / 2, datetime.now(UTC)


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
    url: str, *, http: httpx.Client, repetitions: int = 10
) -> tuple[float, float, datetime]:
    """Use a persistent HTTP connection to bound remote UTC offset uncertainty."""
    samples = []
    for _ in range(repetitions):
        before = time.time()
        response = http.get(url + "/clock", timeout=1)
        after = time.time()
        response.raise_for_status()
        remote = float(response.json()["timestamp"])
        samples.append((after - before, remote - (before + after) / 2))
    rtt, offset = min(samples)
    return offset, rtt / 2, datetime.now(UTC)


def runtime_endpoint(
    node: NodeResource, function: str, *, inputs: TaskInputs, replica: int = 1
) -> tuple[str, dict]:
    """Resolve replica one by verified managed-container labels."""
    from nanolab.one_shot.infrastructure import NodeExecutor
    from nanolab.tasks.managed_containers import inspect_managed_container

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
    ):
        """Bind explicit resource handles and expected physical identities."""
        self.resources, self.endpoints, self.probes = resources, endpoints, probes
        self.distribution, self.function_settings = distribution, function_settings
        self.output, self.max_clock_skew_seconds = output, max_clock_skew_seconds

    def run(self, inputs: TaskInputs) -> TaskOutcome[TopologyEvidence]:
        """Preserve partial observations even when collection or validation fails."""
        import json

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
                        "purpose": "workflow-validation",
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
        import json

        from nanolab.one_shot.client import NanoFaasOneShotClient

        evidence = []
        built = inputs.resource(self.distribution)
        expected_images = {
            component.name: component.image.id
            for component in built.components
            if component.kind == "function"
        }
        with httpx.Client(trust_env=False) as http:
            for key, node in self.resources.nodes.items():
                vm = inputs.resource(node.vm)
                url = inputs.resource(self.endpoints[key])
                probe = inputs.resource(self.probes[key])
                inventory_response = http.get(probe + "/inventory")
                inventory_response.raise_for_status()
                inventory = inventory_response.json()
                self.observations[key] = {"inventory": inventory}
                peer_count = 0
                clock, uncertainty, measured_at = measure_http_clock(probe, http=http)
                if node.config.kind == "edge":
                    client = NanoFaasOneShotClient(url, http=http)
                    client.update_clock_health(
                        offset_seconds=clock, measured_at=measured_at
                    )
                    expected_peers = (
                        sum(
                            other.config.kind == "edge"
                            for other in self.resources.nodes.values()
                        )
                        - 1
                    )
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        status = client.status()
                        peer_count = len(status.peer_endpoints)
                        if peer_count == expected_peers:
                            break
                        time.sleep(0.2)
                    if peer_count != expected_peers:
                        raise ValueError("incomplete peer membership")
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
                for function, expected in self.function_settings.items():
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            runtime, observed = runtime_endpoint(
                                node, function, inputs=inputs
                            )
                            response = http.get(runtime + "/runtime/status", timeout=2)
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
                    spec_response = http.get(url + "/v1/functions/" + function)
                    spec_response.raise_for_status()
                    spec = spec_response.json()
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
                    image_digests=image_digests,
                    clock_offset_seconds=clock,
                    clock_uncertainty_seconds=uncertainty,
                    rtt_seconds=2 * uncertainty,
                    bandwidth_mbps=network["bytes"] * 8 / network["seconds"] / 1e6,
                    os=inventory["os"],
                    architecture=inventory["architecture"],
                )
                validate_node(fact, max_clock_skew_seconds=self.max_clock_skew_seconds)
                if fact.cpus < node.config.cpus:
                    raise ValueError("VM CPU allocation mismatch")
                evidence.append(fact)
        topology = TopologyEvidence(
            measured_at=datetime.now(UTC),
            generator_location=self.resources.generator_location,
            nodes=evidence,
        )
        self.output.write_text(topology.model_dump_json(indent=2) + "\n")
        return TaskOutcome(value=topology)
