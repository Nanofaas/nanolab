"""Shared type aliases used across the nanolab packages."""

from typing import Literal

FunctionRuntimeKind = Literal[
    "java", "java-lite", "go", "python", "exec", "javascript", "fixture"
]
