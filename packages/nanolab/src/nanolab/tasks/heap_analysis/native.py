"""Summarize the optional native memory readings without inferring a cause.

Process residency uses the kernel page categories. VMA backing labels describe
mappings, not every resident page inside them; no bytes are attributed to an
allocator.
"""

from __future__ import annotations

import re
from typing import Any

# A descriptive threshold on virtual mapping size, not an allocator signature.
# JVM mappings can pass it; reserved size and residency are distinct.
LARGE_MAPPING_BYTES = 33554432

_KB = re.compile(r"^(\w+):\s+(\d+) kB$", re.MULTILINE)
# JDK 25 G1, the collector this role actually runs (runtime_options is empty
# and the helper is eclipse-temurin:25-jdk, which diagnostic_helper refuses
# out of unless both versions start with "25."). Real output, from
# `jcmd <pid> GC.heap_info` on 25.0.4:
#   garbage-first heap   total reserved 1048576K, committed 264192K, used 27268K [...]
# `total reserved` is a reservation, not a commitment, so it is deliberately
# not read: committed is reported only when the output says `committed`.
_HEAP = re.compile(
    r"garbage-first heap\s+total reserved \d+K,\s+committed\s+(\d+)K,"
    r"\s+used\s+(\d+)K"
)
# Verified on JDK 25.0.4 (build 25.0.4+7-1-24.04-Ubuntu): no collector's
# GC.heap_info output carries a Metaspace line (`grep -c Metaspace` is 0 for
# G1, Serial and Parallel), so this cannot match a real capture today and the
# spec's "the same pair for metaspace" from heap_info is unreachable through
# this command -- metaspace needs VM.metaspace or NMT, and NMT is excluded.
# Kept for a future JDK or flag that does print it; on JDK 25 metaspace stays
# absent, never zero.
_METASPACE = re.compile(r"Metaspace\s+used (\d+)K, committed (\d+)K")
_HEADER = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+)[ \t]+([rwxps-]{4})[ \t]+"
    r"[0-9a-f]+[ \t]+([0-9a-f]+:[0-9a-f]+)[ \t]+([0-9]+)"
    r"(?:[ \t]+(.*))?$",
    re.MULTILINE,
)
_RESIDENCY = ("RssAnon", "RssFile", "RssShmem", "Pss_Anon", "Pss_File", "Pss_Shmem")


def _kilobytes(value: str) -> int:
    return int(value) * 1024


def parse_heap_info(text: str) -> dict[str, dict[str, int] | None]:
    """Normalize committed/used to bytes; unrecognized output stays unavailable."""
    heap = _HEAP.search(text)
    metaspace = _METASPACE.search(text)
    return {
        "heap": (
            {"committed": _kilobytes(heap[1]), "used": _kilobytes(heap[2])}
            if heap
            else None
        ),
        "metaspace": (
            {
                "committed": _kilobytes(metaspace[2]),
                "used": _kilobytes(metaspace[1]),
            }
            if metaspace
            else None
        ),
    }


def _backing(path: str, permissions: str) -> str:
    """Describe VMA backing, never the backing of every resident page."""
    # /dev/shm/ is a procfs path prefix in a mapping header, not a temp
    # directory this code creates; bandit matches the literal by name.
    if path.startswith(
        ("[anon_shmem:", "/dev/shm/", "/memfd:", "/SYSV")  # nosec B108
    ) or (not path and permissions.endswith("s")):
        return "shared_memory"
    if permissions.endswith("p") and (
        not path
        or path in {"[heap]", "[stack]"}
        or path.startswith(("[anon:", "[stack:"))
    ):
        return "anonymous"
    if path.startswith("/"):
        return "file"
    return "unknown"


def parse_smaps(text: str) -> dict[str, Any]:
    """Reject incomplete records and separate VMA backing from page residency."""
    headers = list(_HEADER.finditer(text))
    if not headers or text[: headers[0].start()].strip():
        raise ValueError("smaps has no complete mapping header")
    totals = {
        name: {"size": 0, "rss": 0, "pss": 0}
        for name in ("anonymous", "file", "shared_memory", "unknown")
    }
    records, large = [], []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        body = text[header.end() : end]
        # A malformed subsequent header must not silently join this record.
        if re.search(r"^[0-9a-f]+-", body, re.MULTILINE):
            raise ValueError("smaps contains an unrecognized mapping header")
        pairs = _KB.findall(body)
        fields = dict(pairs)
        required = {"Size", "Rss", "Pss"}
        if any(sum(name == key for name, _ in pairs) != 1 for key in required):
            raise ValueError("smaps mapping has missing or duplicate Size/Rss/Pss")
        if int(header[2], 16) <= int(header[1], 16):
            raise ValueError("invalid smaps address range")
        path = (header[6] or "").strip()
        backed = _backing(path, header[3])
        values = {
            "size": _kilobytes(fields["Size"]),
            "rss": _kilobytes(fields["Rss"]),
            "pss": _kilobytes(fields["Pss"]),
        }
        record = {
            "address": f"{header[1]}-{header[2]}",
            "permissions": header[3],
            "backing": backed,
            "path": path,
            **values,
        }
        records.append(record)
        for key, value in values.items():
            totals[backed][key] += value
        if backed == "anonymous" and values["size"] >= LARGE_MAPPING_BYTES:
            large.append(record)
    return {
        "mappings": len(records),
        "mapping_details": records,
        **totals,
        "large_anonymous_mappings": {
            "count": len(large),
            "size": sum(item["size"] for item in large),
            "rss": sum(item["rss"] for item in large),
            "pss": sum(item["pss"] for item in large),
            "mappings": large,
        },
    }


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
