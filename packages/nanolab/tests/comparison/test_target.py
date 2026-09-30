"""Resume requires original machines and the original Kubernetes cluster."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import cast

import pytest
from sonata_tasks.vm.ports import VmCommandProvider

from nanolab.comparison.target import read_comparison_target, require_comparison_target
from nanolab.tasks.vm.models import VmRequest


class IdentityProvider:
    def __init__(self):
        self.calls = []
        self.ensure_calls = []
        self.machine = "a" * 32
        self.product = "11111111-2222-3333-4444-555555555555"
        self.cluster = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.failure = False

    def exec_argv(self, request, argv, **kwargs):
        self.calls.append((request.name, argv))
        if argv == ("cat", "/etc/machine-id"):
            stdout = self.machine + "\n"
        elif argv == ("sudo", "cat", "/sys/class/dmi/id/product_uuid"):
            stdout = self.product + "\n"
        elif "namespace" in argv:
            stdout = json.dumps({"metadata": {"uid": self.cluster}})
        elif "nodes" in argv:
            stdout = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {
                                "name": "stack",
                                "uid": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
                            }
                        }
                    ]
                }
            )
        else:
            pytest.fail(f"unexpected mutating/probe command: {argv}")
        return SimpleNamespace(
            return_code=1 if self.failure else 0,
            stdout=stdout,
            stderr="failure" if self.failure else "",
        )


def probe(provider):
    return read_comparison_target(
        cast(VmCommandProvider, provider),
        {
            "stack": VmRequest(lifecycle="multipass", name="same-name"),
            "loadgen": VmRequest(lifecycle="multipass", name="loadgen"),
        },
    )


def test_same_name_replacement_vm_is_not_the_original():
    provider = IdentityProvider()
    original = probe(provider)
    provider.machine = "b" * 32
    replacement = probe(provider)
    with pytest.raises(ValueError, match="identity"):
        require_comparison_target(original, replacement)
    assert provider.ensure_calls == []


def test_recreated_cluster_is_rejected_on_original_machine():
    provider = IdentityProvider()
    original = probe(provider)
    provider.cluster = "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"
    with pytest.raises(ValueError, match="identity"):
        require_comparison_target(original, probe(provider))


def test_stable_target_is_valid_after_reboot():
    provider = IdentityProvider()
    original = probe(provider)
    require_comparison_target(original, probe(provider))
    assert isinstance(original["machines"], dict)
    assert isinstance(original["cluster"], dict)
    assert original["machines"]["stack"]["machineId"] == "a" * 32
    assert original["machines"]["loadgen"]["productUuid"] == provider.product
    assert original["cluster"]["namespaceUid"] == provider.cluster
    assert original["cluster"]["nodes"] == {
        "stack": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    }
    assert provider.ensure_calls == []


@pytest.mark.parametrize("field", ["machine", "product", "cluster", "failure"])
def test_missing_or_failed_identity_probe_is_not_recoverable(field):
    provider = IdentityProvider()
    setattr(provider, field, True if field == "failure" else "")
    with pytest.raises((ValueError, RuntimeError), match="identity"):
        probe(provider)


def test_incomplete_identity_is_rejected_even_if_both_records_match():
    record = {"machines": {"stack": {"name": "same-name"}}, "cluster": {}}
    with pytest.raises(ValueError, match="identity"):
        require_comparison_target(record, deepcopy(record))
