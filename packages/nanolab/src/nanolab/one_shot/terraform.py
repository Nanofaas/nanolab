"""One owned OpenTofu stack per Multipass site, consumed by existing node tasks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from importlib.resources import files
from ipaddress import IPv4Address
from pathlib import Path

from multipass_vm_sdk import find_ssh_public_key
from sonata_tasks.execution.local import LocalCommandTaskExecutor
from sonata_tasks.terraform import TerraformSpec, terraform_stack_resource

from nanolab.one_shot.sites import SiteHost
from nanolab.tasks.one_shot.artifacts import canonical_bytes, write_immutable_artifact
from nanolab.tasks.resource_tasks import resource_of
from nanolab.tasks.vm.models import VmInfo, vm_remote_home


def validate_nodes(values, requests) -> dict[str, VmInfo]:
    """Validate a complete fleet before any node can be bootstrapped."""
    if not isinstance(values, dict) or set(values) != set(requests):
        raise ValueError("Terraform outputs must name exactly the requested nodes")
    result = {}
    seen = set()
    for key, request in requests.items():
        row = values[key]
        if not isinstance(row, dict) or row.get("name") != request.name:
            raise ValueError("Terraform VM identity differs from the requested node")
        addresses = row.get("ipv4")
        if (
            not isinstance(addresses, list)
            or not addresses
            or not isinstance(addresses[0], str)
        ):
            raise ValueError("Terraform node has no usable IPv4 address")
        try:
            address = IPv4Address(addresses[0])
        except ValueError:
            raise ValueError("Terraform node IPv4 address is invalid") from None
        if (
            address.is_unspecified
            or address.is_loopback
            or address.is_multicast
            or str(address) in seen
        ):
            raise ValueError(
                "Terraform node addresses must be distinct guest addresses"
            )
        seen.add(str(address))
        result[key] = VmInfo(
            request.name, str(address), request.user, vm_remote_home(request)
        )
    return result


def collection_complete(run_dir: Path, node) -> bool:
    """Require the existing collector receipt and the actual verified tarball."""
    if node.recovery:
        return False
    directory = run_dir / "logging" / node.config.id
    try:
        status = json.loads((directory / "status.json").read_text())
        hashes = json.loads((directory / "sha256.json").read_text())
        archive = directory / "collector-state.tar.gz"
        return (
            status.get("stateArchive") == "verified"
            and hashes.get(archive.name)
            == hashlib.sha256(archive.read_bytes()).hexdigest()
        )
    except (OSError, ValueError, AttributeError):
        return False


def check_disk_capacity(requests, site, storage_path: str | None) -> None:
    """Reserve requested guest disks and 5 GiB headroom on the storage filesystem."""
    required = sum(int(r.disk.removesuffix("G")) for r in requests.values()) * 1024**3
    if site is None:
        free = shutil.disk_usage(
            Path(storage_path).expanduser() if storage_path else Path.home()
        ).free
    else:
        path = storage_path or "/var/snap/multipass/common/data/multipassd"
        output = SiteHost(site).run(("df", "-B1", "--output=avail", path)).stdout
        free = int(output.splitlines()[-1].strip())
    if required + 5 * 1024**3 > free:
        raise ValueError(
            "Terraform VM disks exceed available storage plus required headroom"
        )


def _record(path: Path, value) -> None:
    write_immutable_artifact(path, canonical_bytes(value))
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_fleet(
    *,
    config,
    environment,
    namespace: str,
    name: str,
    requests,
    provider,
    run_dir: Path,
    resources,
    site=None,
):
    """Prepare a pinned module and return VM bindings depending on its stack.

    ``resources`` is the builder's node map, populated after these bindings
    are returned. The release guard reads its recovery flags and durable
    collection receipts, including after process restart.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise ValueError("invalid Terraform site name")
    directory = Path(run_dir) / "infrastructure" / name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner = directory / ".sonata-terraform" / "owner.json"
    options = environment.opentofu
    if not owner.exists():
        for request in requests.values():
            if provider.vm_exists(request):
                raise RuntimeError(f"refusing to adopt existing VM {request.name}")
        check_disk_capacity(requests, site, options.storage_path)
    if site is None:
        public_key = find_ssh_public_key()
        host = user = None
    else:
        public_key = (
            Path(str(Path(site.vm_ssh_key).expanduser()) + ".pub").read_text().strip()
        )
        user, separator, host = site.ssh.rpartition("@")
        if not separator:
            host, user = site.ssh, None
    if not public_key:
        raise ValueError("OpenTofu VM provisioning requires an SSH public key")
    assets = files("nanolab").joinpath("assets/terraform/multipass")
    for filename in ("main.tf", ".terraform.lock.hcl"):
        write_immutable_artifact(
            directory / filename, assets.joinpath(filename).read_bytes()
        )
    variables = {
        "ssh_host": host,
        "ssh_user": user,
        "multipass_path": site.multipass if site else "multipass",
        "ssh_public_key": public_key,
        "nodes": {
            key: {"name": r.name, "cpus": r.cpus, "memory": r.memory, "disk": r.disk}
            for key, r in requests.items()
        },
    }
    write_immutable_artifact(
        directory / "nodes.auto.tfvars.json", canonical_bytes(variables)
    )
    env = (
        {
            "TF_CLI_CONFIG_FILE": str(
                Path(options.cli_config_file).expanduser().resolve()
            )
        }
        if options.cli_config_file
        else {}
    )
    spec = TerraformSpec(
        directory,
        owner=f"{namespace}-{name}",
        inputs=("main.tf", ".terraform.lock.hcl", "nodes.auto.tfvars.json"),
        outputs=("nodes",),
        binary=options.binary,
        env=env,
    )
    markers = directory / "collection"
    logs_enabled = config.observability.logs.enabled

    def retain():
        for key in requests:
            node = resources.get(key)
            if node is not None and node.recovery:
                return f"{key}: raw recovery requested"
            active = markers / f"{key}.active.json"
            done = markers / f"{key}.done.json"
            if active.exists():
                try:
                    if json.loads(done.read_text()) != {"vm": requests[key].name}:
                        return f"{key}: collection completion missing"
                except (OSError, ValueError):
                    return f"{key}: collection completion missing"
                if logs_enabled and (
                    node is None or not collection_complete(run_dir, node)
                ):
                    return f"{key}: verified archive unavailable"
        return None

    stack = terraform_stack_resource(
        spec, executor=LocalCommandTaskExecutor(), retain=retain
    )

    def binding(key):
        def acquire(inputs):
            if (markers / f"{key}.done.json").exists():
                raise RuntimeError(
                    "Terraform node was already collected; "
                    "use a new run or explicit teardown"
                )
            values = validate_nodes(
                inputs.resource(stack).outputs.get("nodes"), requests
            )
            _record(markers / f"{key}.active.json", {"vm": requests[key].name})
            return values[key]

        def release(_inputs, _value):
            node = resources[key]
            if node.recovery or (
                logs_enabled and not collection_complete(run_dir, node)
            ):
                raise RuntimeError(
                    f"{key}: Terraform stack retained until collection is verified"
                )
            _record(markers / f"{key}.done.json", {"vm": requests[key].name})

        return resource_of(
            f"Acquire {key} stack VM",
            acquire,
            release,
            requires=(stack,),
            idempotent=True,
        )

    return {key: binding(key) for key in requests}
