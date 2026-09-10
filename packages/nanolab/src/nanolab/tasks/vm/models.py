"""VM request models, extending the shared request with nanolab's own fields."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from sonata_tasks.vm.models import (
    VmConfig,
    VmInfo,
    VmLifecycle,
    vm_remote_home,
)
from sonata_tasks.vm.models import (
    VmRequest as SharedVmRequest,
)


class NanolabVmRequest(SharedVmRequest):
    """A shared VM request plus the flag that skips rendering a zero-replica HPA."""

    hpa_scale_to_zero: bool = False


VmRequest = NanolabVmRequest


class _VmEnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True)
    e2e_vm_lifecycle: VmLifecycle = "multipass"
    vm_name: str | None = None
    e2e_vm_host: str | None = None
    e2e_vm_user: str = "ubuntu"
    e2e_vm_home: str | None = None
    cpus: int = 4
    memory: str = "12G"
    disk: str = "30G"


def vm_request_from_env() -> NanolabVmRequest:
    """Build a VM request from the `E2E_VM_*` and size variables in the environment."""
    settings = _VmEnvSettings()
    return NanolabVmRequest(
        lifecycle=settings.e2e_vm_lifecycle,
        name=settings.vm_name,
        host=settings.e2e_vm_host,
        user=settings.e2e_vm_user,
        home=settings.e2e_vm_home,
        cpus=settings.cpus,
        memory=settings.memory,
        disk=settings.disk,
    )


__all__ = [
    "NanolabVmRequest",
    "VmConfig",
    "VmInfo",
    "VmLifecycle",
    "VmRequest",
    "vm_remote_home",
    "vm_request_from_env",
]
