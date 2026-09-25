"""Image names and the build plan that produces them on the VM."""

from __future__ import annotations


def control_image(local_registry: str) -> str:
    """Return the control-plane image tag published on `local_registry`."""
    return f"{local_registry}/nanofaas/control-plane:e2e"
