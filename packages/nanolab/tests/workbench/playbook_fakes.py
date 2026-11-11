"""Read the public Ansible task arguments in lifecycle tests."""

from pathlib import Path


def action_of(task):
    return Path(task.argv[-1]).stem


def inventory_of(task):
    return task.argv[task.argv.index("-i") + 1]


def host_of(task):
    return Path(inventory_of(task)).name.removesuffix(
        f"-{action_of(task)}-inventory.json"
    )
