"""Fleet ownership, connection facts and evidence retention."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from sonata_engine import TaskInputs, Workflow
from sonata_tasks.terraform import TerraformResult

from nanolab.config import EnvironmentConfig
from nanolab.one_shot import terraform
from nanolab.one_shot.infrastructure import _owned_node
from nanolab.tasks.resource_tasks import call, resource_of
from nanolab.tasks.vm.models import VmInfo, VmRequest
from tests.one_shot.test_resources import config


def environment(**kwargs):
    return EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "test"}}, **kwargs}
    )


def test_configuration_keeps_direct_default_and_accepts_explicit_opentofu():
    assert environment().provisioning == "direct"
    assert (
        environment(
            provisioning="opentofu", opentofu={"binary": "tofu"}
        ).opentofu.binary
        == "tofu"
    )


def test_mapping_validates_exact_nodes_and_connection_identity():
    requests = {"edge": VmRequest(lifecycle="multipass", name="run-edge")}
    values = {"edge": {"name": "run-edge", "ipv4": ["10.0.0.2"]}}
    assert terraform.validate_nodes(values, requests) == {
        "edge": VmInfo("run-edge", "10.0.0.2", "ubuntu", "/home/ubuntu")
    }


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"other": {}},
        {"edge": {"name": "foreign", "ipv4": ["10.0.0.2"]}},
        {"edge": {"name": "run-edge", "ipv4": []}},
        {"edge": {"name": "run-edge", "ipv4": ["not-an-ip"]}},
    ],
)
def test_mapping_rejects_incomplete_or_foreign_outputs(values):
    with pytest.raises(ValueError, match="Terraform"):
        terraform.validate_nodes(
            values, {"edge": VmRequest(lifecycle="multipass", name="run-edge")}
        )


def test_collection_requires_verified_archive_and_no_recovery(tmp_path):
    node = SimpleNamespace(config=SimpleNamespace(id="edge"), recovery=[])
    assert not terraform.collection_complete(tmp_path, node)
    directory = tmp_path / "logging/edge"
    directory.mkdir(parents=True)
    archive = directory / "collector-state.tar.gz"
    archive.write_bytes(b"raw evidence")
    (directory / "status.json").write_text('{"stateArchive":"verified"}')
    (directory / "sha256.json").write_text(
        json.dumps(
            {"collector-state.tar.gz": hashlib.sha256(b"raw evidence").hexdigest()}
        )
    )
    assert terraform.collection_complete(tmp_path, node)
    archive.write_bytes(b"tampered")
    assert not terraform.collection_complete(tmp_path, node)


def test_borrowed_node_never_launches_or_deletes_vm(tmp_path):
    def forbidden(*args):
        raise AssertionError("per-node VM lifecycle must not run")

    provider = SimpleNamespace(
        vm_exists=forbidden, ensure_running=forbidden, teardown=forbidden
    )
    settings = config()
    request = VmRequest(lifecycle="multipass", name="owned-by-stack")
    value = VmInfo("owned-by-stack", "10.0.0.2", "ubuntu", "/home/ubuntu")
    events = []
    fleet = resource_of(
        "Acquire fleet", lambda _: value, lambda *_: events.append("fleet")
    )
    binding = resource_of(
        "Acquire node binding",
        lambda i: i.resource(fleet),
        lambda *_: events.append("node"),
        requires=(fleet,),
    )
    node = _owned_node(
        settings.nodes[0],
        request,
        provider,
        config=settings,
        generator=resource_of(
            "Acquire generator", lambda _: "operator", lambda *_: None
        ),
        target_cpus=None,
        pin=lambda *_: None,
        stop_argv=None,
        acquired_vm=binding,
    )
    workflow = Workflow("borrowed")
    workflow.add(
        call("consume", lambda i: events.append(i.resource(node.vm).host)),
        requires=(node.vm,),
    )
    workflow.run()
    assert events == ["10.0.0.2", "node", "fleet"]


def test_fleet_guard_preserves_started_node_across_reconstruction(
    tmp_path, monkeypatch
):
    captured = []
    requests = {"edge": VmRequest(lifecycle="multipass", name="run-edge")}
    node = SimpleNamespace(config=SimpleNamespace(id="edge"), recovery=[])

    def stack(spec, **kwargs):
        captured.append((spec, kwargs))
        return resource_of(
            "Acquire fake fleet",
            lambda _: TerraformResult(
                outputs={"nodes": {"edge": {"name": "run-edge", "ipv4": ["10.0.0.2"]}}}
            ),
            lambda *_: None,
        )

    monkeypatch.setattr(terraform, "terraform_stack_resource", stack)
    monkeypatch.setattr(
        terraform, "find_ssh_public_key", lambda: "ssh-ed25519 AAAA test"
    )
    monkeypatch.setattr(terraform, "check_disk_capacity", lambda *a: None)
    args = {
        "config": config(),
        "environment": environment(provisioning="opentofu"),
        "namespace": "run",
        "name": "local",
        "requests": requests,
        "provider": SimpleNamespace(vm_exists=lambda _: False),
        "run_dir": tmp_path,
        "resources": {"edge": node},
    }
    bindings = terraform.prepare_fleet(**args)
    fleet = bindings["edge"].requires[0]
    owned = _owned_node(
        config().nodes[0],
        requests["edge"],
        args["provider"],
        config=config(),
        generator=resource_of(
            "Acquire generator", lambda _: "operator", lambda *_: None
        ),
        target_cpus=None,
        pin=lambda *_: None,
        stop_argv=None,
        acquired_vm=bindings["edge"],
    )
    assert owned.vm.revive is not None
    assert owned.vm.revive(
        {
            "name": "run-edge",
            "host": "10.0.0.2",
            "user": "ubuntu",
            "home": "/home/ubuntu",
        }
    ) == VmInfo("run-edge", "10.0.0.2", "ubuntu", "/home/ubuntu")
    value = fleet.acquire.run(TaskInputs.empty()).value
    inputs = TaskInputs._for_resource_values({id(fleet): value}, (fleet,))
    bindings["edge"].acquire.run(inputs)
    assert captured[-1][1]["retain"]() is not None
    terraform.prepare_fleet(**args)
    assert captured[-1][1]["retain"]() is not None


def test_remote_fleet_uses_matching_dotted_ssh_public_key(tmp_path, monkeypatch):
    key = tmp_path / "my.key"
    key.write_text("private fixture")
    public = key.with_name(key.name + ".pub")
    public.write_text("ssh-ed25519 correct-key")
    key.with_suffix(".pub").write_text("ssh-ed25519 wrong-key")
    site = SimpleNamespace(
        vm_ssh_key=str(key), ssh="michele@lab", multipass="multipass"
    )
    monkeypatch.setattr(terraform, "check_disk_capacity", lambda *args: None)
    terraform.prepare_fleet(
        config=config(),
        environment=environment(provisioning="opentofu"),
        namespace="test",
        name="lab",
        requests={"edge": VmRequest(lifecycle="multipass", name="test-edge")},
        provider=SimpleNamespace(vm_exists=lambda _: False),
        run_dir=tmp_path,
        resources={},
        site=site,
    )
    values = json.loads(
        (tmp_path / "infrastructure/lab/nodes.auto.tfvars.json").read_text()
    )
    assert values["ssh_public_key"] == public.read_text()
    assert site.vm_ssh_key == str(key)
