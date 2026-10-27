"""Execution targets a workflow step can be assigned to."""

from typing import Literal

ExecutionRole = Literal["host", "stack", "loadgen", "cloud", "arm-builder"]
