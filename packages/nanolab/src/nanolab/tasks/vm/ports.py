"""VM provider ports re-exported for the nanolab task layer."""

from sonata_tasks.vm.ports import VmCommandProvider, VmLifecycleProtocol

__all__ = ["VmCommandProvider", "VmLifecycleProtocol"]
