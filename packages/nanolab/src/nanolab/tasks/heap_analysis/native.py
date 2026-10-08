"""Summarize the optional native memory readings without inferring a cause.

Process residency uses the kernel page categories. VMA backing labels describe
mappings, not every resident page inside them; no bytes are attributed to an
allocator.
"""

from __future__ import annotations

import re
from typing import Any

from sonata_tasks.procfs import parse_smaps as _parse_smaps

# A descriptive threshold on virtual mapping size, not an allocator signature.
# JVM mappings can pass it; reserved size and residency are distinct.
LARGE_MAPPING_BYTES = 33554432

_KB = re.compile(r"^(\w+):\s+(\d+) kB$", re.MULTILINE)
# `GC.heap_info` prints a different shape per collector, and the collector is
# chosen by the IMAGE's JVM_TUNING, not by the scenario: control-plane variants
# default to `-XX:+UseSerialGC -XX:TieredStopAtLevel=1` and others build with
# G1 (see nanolab/images/control_plane_variants.py). A host capture cannot tell
# you what the image runs -- the first real heap-analysis run recorded Serial,
# which a G1-only pattern matched on the host and not in the container. Both
# shapes below are real JDK 25.0.4 captures.
#
# G1 prints one summary line with an explicit commitment:
#   garbage-first heap   total reserved 1048576K, committed 264192K, used 27268K [...]
# `total reserved` is a reservation, not a commitment, so it is deliberately
# not read: committed is reported only when the output says `committed`.
_HEAP = re.compile(
    r"garbage-first heap\s+total reserved \d+K,\s+committed\s+(\d+)K,"
    r"\s+used\s+(\d+)K"
)
# Serial prints one line per generation, where `total` is that space's
# committed capacity -- the quantity G1 spells `committed`:
#   DefNew     total 20992K, used 2955K [...]
#   Tenured    total 46480K, used 31915K [...]
_SERIAL_SPACES = re.compile(
    r"^(DefNew|Tenured)\s+total (\d+)K,\s+used (\d+)K", re.MULTILINE
)
# Verified on JDK 25.0.4 (build 25.0.4+7-1-24.04-Ubuntu): no collector's
# GC.heap_info output carries a Metaspace line (`grep -c Metaspace` is 0 for
# G1, Serial and Parallel), so this cannot match a real capture today and the
# spec's "the same pair for metaspace" from heap_info is unreachable through
# this command -- metaspace needs VM.metaspace or NMT, and NMT is not read here
# (the soak operation `native_memory` captures VM.native_memory for scenarios
# that set the flag).
# Kept for a future JDK or flag that does print it; on JDK 25 metaspace stays
# absent, never zero.
_METASPACE = re.compile(r"Metaspace\s+used (\d+)K, committed (\d+)K")
_RESIDENCY = ("RssAnon", "RssFile", "RssShmem", "Pss_Anon", "Pss_File", "Pss_Shmem")


def _kilobytes(value: str) -> int:
    return int(value) * 1024


def _heap_committed_used(text: str) -> dict[str, int] | None:
    """Read committed and used whichever shape this collector prints, or nothing.

    A shape that is not recognized yields nothing rather than a partial total:
    for Serial both generations are required, so a read cut mid-output cannot
    publish a heap figure that looks complete.
    """
    summary = _HEAP.search(text)
    if summary:
        return {"committed": _kilobytes(summary[1]), "used": _kilobytes(summary[2])}
    spaces = _SERIAL_SPACES.findall(text)
    if len(spaces) != 2:
        return None
    return {
        "committed": sum(_kilobytes(total) for _, total, _ in spaces),
        "used": sum(_kilobytes(used) for _, _, used in spaces),
    }


def parse_heap_info(text: str) -> dict[str, dict[str, int] | None]:
    """Normalize committed/used to bytes; unrecognized output stays unavailable."""
    metaspace = _METASPACE.search(text)
    return {
        "heap": _heap_committed_used(text),
        "metaspace": (
            {
                "committed": _kilobytes(metaspace[2]),
                "used": _kilobytes(metaspace[1]),
            }
            if metaspace
            else None
        ),
    }


def parse_smaps(text: str) -> dict[str, Any]:
    """Add the product's large-mapping view to shared VMA backing totals."""
    parsed: dict[str, Any] = dict(_parse_smaps(text))
    large = [
        mapping
        for mapping in parsed["mapping_details"]
        if mapping["backing"] == "anonymous" and mapping["size"] >= LARGE_MAPPING_BYTES
    ]
    parsed["large_anonymous_mappings"] = {
        "count": len(large),
        "size": sum(mapping["size"] for mapping in large),
        "rss": sum(mapping["rss"] for mapping in large),
        "pss": sum(mapping["pss"] for mapping in large),
        "mappings": large,
    }
    return parsed


def residency(status: str | None, rollup: str | None) -> dict[str, int]:
    """Keep the kernel's own categories; a missing field stays absent, not zero."""
    values = dict(_KB.findall(status or "")) | dict(_KB.findall(rollup or ""))
    return {name: _kilobytes(values[name]) for name in _RESIDENCY if name in values}


def _source(response: dict, name: str) -> tuple[bool, str | None]:
    error = (response.get("errors") or {}).get(name)
    return response.get(name) is not None and error is None, error


def summarize(response: dict) -> dict[str, Any]:
    """Build the native block, publishing no total derived from a failed source."""
    block: dict[str, Any] = {
        "residency": residency(response.get("status"), response.get("smaps_rollup"))
    }
    for name, parse in (("smaps", parse_smaps), ("heap_info", parse_heap_info)):
        available, error = _source(response, name)
        if available:
            try:
                parsed = parse(response[name])
            except ValueError as parse_error:
                block[name] = {"available": False, "error": str(parse_error)}
            else:
                recognized = name != "heap_info" or any(
                    value is not None for value in parsed.values()
                )
                if recognized:
                    block[name] = {"available": True, **parsed}
                elif name == "heap_info" and response[name].strip():
                    # Present but nothing parsed: say so. A silently
                    # unavailable reading is how a format change hides.
                    block[name] = {
                        "available": False,
                        "error": "unrecognized GC.heap_info output",
                    }
                else:
                    block[name] = {"available": False, **parsed}
        elif error is not None:
            block[name] = {"available": False, "error": error}
        else:
            block[name] = {"available": False}
    return block
